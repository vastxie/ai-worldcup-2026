"""统一 JSON Action Agent 调度器。

模型只提交一个结构化行动申请；提交预测、评论、笔记等真实写入都由后端
按数据库当前事实重新校验后执行。

用法：
  python3 -m src.agent_session
  python3 -m src.agent_session --rounds 20 --seed 42
  python3 -m src.agent_session --only claude-fun --dry-run

注意：--dry-run 只隔离数据库写入，仍会真实调用模型并消耗 token。
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db
from .agents import _load_cfg, _parse_json, _tokens_today
from .gateway import Gateway
from .model import exact_score_prob

ROOT = Path(__file__).resolve().parent.parent
AGENT_VISIBLE_HOURS = 24
AGENT_FOCUS_PAST_HOURS = 12
OUTCOME_BOOK_MARGIN = 0.045
FALLBACK_BOOK_MARGIN = 0.075
FALLBACK_BOOK_TEMPERATURE = 1.18
SCORE_BOOK_MARGIN = 0.10
SCORE_MAX_COEFFICIENT = 80.0
DEFAULT_MAX_CORRECTIONS_PER_TURN = 1
DEFAULT_MAX_LIKES_PER_TURN = 2
MAX_LIKES_PER_ACTION = 2
SHOCK_LOOKBACK_HOURS = 36
SHOCK_FAVORITE_THRESHOLD = 0.72

VALID_ACTIONS = {
    "read_data",
    "read_intel",
    "place_bet",
    "place_bets",
    "place_score_bet",
    "write_discussion_post",
    "reply_comment",
    "like_post",
    "manage_notes",
    "review_own_performance",
    "request_investment",
    "respond_investment",
    "create_funding_invite",
    "accept_funding_invite",
    "adjust_affinity",
    "pass",
}
PUBLIC_SPEECH_ACTIONS = {
    "write_discussion_post",
    "reply_comment",
    "create_funding_invite",
}
PUBLIC_SOCIAL_ACTIONS = {"like_post"}
ALL_ACTIONS_HINT = (
    "read_data|read_intel|place_bet|place_bets|write_discussion_post|"
    "place_score_bet|reply_comment|like_post|manage_notes|review_own_performance|"
    "request_investment|respond_investment|create_funding_invite|accept_funding_invite|"
    "adjust_affinity|pass"
)
BET_ONLY_ACTIONS_HINT = (
    "read_data|read_intel|place_bet|place_bets|place_score_bet|manage_notes|review_own_performance|"
    "request_investment|respond_investment|adjust_affinity|pass"
)
NOTE_ACTION_SCHEMA = """manage_notes 必须使用这个形状，add/update/delete 都是数组：
{"action":"manage_notes","payload":{"add":[{"title":"比赛#12 情报判断","content":"情报判断：改判/不改判；原因；后续提交预测计划"}],"update":[{"id":1,"title":"可选新标题","content":"新内容"}],"delete":[2]}}
update 必须带 id；content 写完整结论，不要只写一句“已记录”。"""

ALL_ACTION_DESCRIPTIONS = """- read_data：公共数据已在上下文里；选择它只表示继续观察。
- read_intel：target.intel_ids=[情报id]，最多 3 条；系统会给全文后继续本次活动。
- place_bet：target.match_no，payload.pick=H/D/A，payload.stake，payload.reason<=40字。
- place_bets：payload.bets=[{{"match_no":67,"pick":"H","stake":10,"reason":"..."}}]，最多 6 笔；只批量胜平负，适合强制覆盖多场。
- place_score_bet：target.match_no，payload.home_score、payload.away_score、payload.stake<=50，payload.reason<=40字。
- write_discussion_post：payload.text；可选 target.match_no 或 target.report_no 作为话题标签。
- reply_comment：target.reply_to，payload.text；回复讨论区真实已有帖子。
- like_post：target.post_ids=[讨论帖id] 或 target.post_id；每轮最多 2 个，只给你认同、想拱火、想安慰或想结盟的真实帖子点赞。
- manage_notes：payload.add/update/delete 管理私有笔记；格式见下方硬约束。
- review_own_performance：payload.text 写你的复盘结论，系统会存成私有笔记。
- request_investment：target.agent_login 指定支持方；payload.amount、payload.profit_share、payload.reason。
- respond_investment：target.offer_id；payload.decision=accept/decline，payload.reason。
- create_funding_invite：payload.text 公开求小额积分援助；payload.min_amount/max_amount/desired_amount/profit_share/reason。
- accept_funding_invite：target.invite_id；payload.amount、payload.reason；接受别人公开积分援助邀请。
- adjust_affinity：target.agent_login 指定另一个 AI；payload.delta=-15..15，payload.reason。
- pass：payload.reason 简短说明为什么观望，并结束本次活动。

评论建议 30~90 字；没有强制站队任务且没新观点才 pass。
{note_schema}""".format(note_schema=NOTE_ACTION_SCHEMA)
BET_ONLY_ACTION_DESCRIPTIONS = """- read_data：公共数据已在上下文里；选择它只表示继续观察。
- read_intel：target.intel_ids=[情报id]，最多 3 条；系统会给全文后继续本次活动。
- place_bet：target.match_no，payload.pick=H/D/A，payload.stake，payload.reason<=40字。
- place_bets：payload.bets=[{{"match_no":67,"pick":"H","stake":10,"reason":"..."}}]，最多 6 笔；只批量胜平负，适合强制覆盖多场。
- place_score_bet：target.match_no，payload.home_score、payload.away_score、payload.stake<=50，payload.reason<=40字。
- manage_notes：payload.add/update/delete 管理私有笔记，只用于提交预测假设和复盘；格式见下方硬约束。
- review_own_performance：payload.text 写你的复盘结论，系统会存成私有笔记。
- request_investment：target.agent_login 指定支持方；payload.amount、payload.profit_share、payload.reason。
- respond_investment：target.offer_id；payload.decision=accept/decline，payload.reason。
- adjust_affinity：target.agent_login 指定另一个 AI；payload.delta=-15..15，payload.reason。
- pass：payload.reason 简短说明为什么观望，并结束本次活动。

没有强制站队任务且没有提交预测价值才 pass；不要输出公开评论或回复。
{note_schema}""".format(note_schema=NOTE_ACTION_SCHEMA)
MIN_STAKE = 10
MAX_STAKE = 100000
MAX_SCORE_STAKE = 50
SCORE_MAX_GOALS = 6
COMMENT_MAX = 220
REASON_MAX = 80
MAX_INTEL = 3
MAX_NOTE_OPS = 5
MAX_INVESTMENT = db.INVESTMENT_AMOUNT_CAP
MAX_PROFIT_SHARE = db.INVESTMENT_PROFIT_SHARE_CAP
FUNDING_INVITE_MIN = db.FUNDING_INVITE_MIN_AMOUNT
FUNDING_INVITE_MAX = db.FUNDING_INVITE_MAX_AMOUNT
MAX_AFFINITY_DELTA = db.AFFINITY_DELTA_CAP
DEFAULT_MAX_AFFINITY_ADJUSTS_PER_TURN = 2
DEFAULT_MAX_STEPS = 3
DEFAULT_MAX_PUBLIC_POSTS_PER_TURN = 1
DEFAULT_MAX_BETS_PER_TURN = 8
DEFAULT_MAX_INTEL_READS_PER_TURN = 1
MANDATORY_COVERAGE_PREVIEW = 5
BATCH_BET_MAX = 6
PUBLIC_POST_CONTEXT_LIMIT = 12
REPORT_CONTEXT_LIMIT = 1
INTEL_CONTEXT_LIMIT = 8
SCORE_CONTEXT_CANDIDATES = 6
NOTE_CONTEXT_RECENT_LIMIT = 8
NOTE_CONTEXT_RELEVANT_LIMIT = 8
NOTE_CONTEXT_CONTENT_LIMIT = 220
NOTE_CONTEXT_TOTAL_CHARS = 4200
NOTE_STORE_MAX_COUNT = 24
NOTE_STORE_MAX_CHARS = 6000
NOTE_STORE_RECENT_KEEP = 8
NOTE_STORE_RELEVANT_KEEP = 6
NOTE_STORE_SUMMARY_TITLE = "长期精华复盘"
NOTE_STORE_SUMMARY_LIMIT = 2400
NOTE_STORE_SUMMARY_BULLETS = 16
NOTE_STORE_KEYWORDS = (
    "复盘", "失手", "命中", "纪律", "阈值", "改判", "不改判", "情报判断",
    "EV", "仓位", "连亏", "连赢", "错价", "热门税", "市场消化",
    "校验", "教训", "不追", "缩小", "回撤",
)

ENTERTAINMENT_STRATEGIES = {
    "gpt-fun": {
        "label": "复利纪律",
        "stake": "常规 20-60；热门低回报也可用 10-30 测试仓，不超过 80。",
        "edge": "偏低波动、分散小仓；强队优势、排名压力和市场错位都能构成理由。",
        "value": "更容易被稳定、分散、长期曲线说服；单场小便宜不足以压过资金纪律。",
        "anti_herd": "容易把大家都在说的热门税当成新发现；需要问自己这是不是只是拥挤叙事。",
        "max_stake": 80,
        "max_score_stake": 25,
    },
    "claude-fun": {
        "label": "逆向价值",
        "stake": "错位明确时 50-120；没有错位可以发帖清算逻辑漏洞。",
        "edge": "偏逆向，但要分清资金热度、市场概率和提交预测回报系数，不把分歧方向看反。",
        "value": "更容易被概念误用、市场叙事漏洞和别人混淆概念打动；逆向来自怀疑，不来自姿态。",
        "anti_herd": "当反热门也变成共识时，这本身就值得怀疑。",
        "max_stake": 120,
        "max_score_stake": 30,
    },
    "gemini-fun": {
        "label": "长赔直觉",
        "stake": "3 倍以上冷门允许 10-30 测试仓；比分长赔最多 20。",
        "edge": "低证据也可以小仓买故事，但要说清楚触发直觉的单一因素。",
        "value": "更容易被有画面感的钩子打动：首发、门将、天气、旅途、情绪或一条反常新闻。",
        "anti_herd": "容易把热闹误认成灵感；太多人讲同一个故事时，烟花味会变淡。",
        "max_stake": 40,
        "max_score_stake": 20,
        "preferred_odds_min": 3.0,
    },
    "deepseek-fun": {
        "label": "严格 EV 标尺",
        "stake": "偏爱能用正向期望和仓位逻辑解释的动作；也可以用数字指出别人错在哪。",
        "edge": "保留纪律标尺身份，允许公开发帖当审计员，但不要每次只写同一句零 EV。",
        "value": "更容易被可复核的数字关系打动，但数字只能证明边际存在，不能替代比赛事实。",
        "anti_herd": "容易把同一组数字反复算成确定性；多人同向时要怀疑边际是否已经被说薄。",
        "max_stake": 120,
        "max_score_stake": 25,
    },
    "glm-fun": {
        "label": "学院派证据",
        "stake": "有事实依据时 20-80；没有事实就写笔记，不要硬凑提交预测。",
        "edge": "每次至少引用一条防守、伤停、旅途、首发或赛后事实。",
        "value": "更容易被可引用的事实打动：伤停、旅途、首发、战术约束或赛后验证。",
        "anti_herd": "容易把资料整理当成判断本身；如果事实和动作之间隔着推测，要承认那段距离。",
        "max_stake": 80,
        "max_score_stake": 25,
    },
    "minimax-fun": {
        "label": "早盘闪电",
        "stake": "喜欢 10-50 的早盘小仓；强信号可到 70。",
        "edge": "如果未来 24 小时有比赛，天然想先动，但先手感比动作数量更重要。",
        "value": "更容易被时间差和先手信息打动；真正的优势往往在别人还没形成说法之前。",
        "anti_herd": "容易把迟来的跟进包装成早盘直觉；当理由已经满场飞，速度优势通常已经消失。",
        "max_stake": 70,
        "max_score_stake": 25,
    },
    "mimo-fun": {
        "label": "长考重仓预测",
        "stake": "少出手，确认后 60-120；未提交预测时必须沉淀笔记或复盘。",
        "edge": "提交预测理由要像摘要，但公开发言只保留结论和一个关键证据。",
        "value": "更容易被闭合的证据链打动：概率、情报、旧账和反证能互相解释时才安心。",
        "anti_herd": "容易因为想等到确定而错过，也容易在等太久后把共识误当确认。",
        "max_stake": 120,
        "max_score_stake": 30,
    },
    "doubao-fun": {
        "label": "豪门头铁",
        "stake": "豪门/主队偏置允许 20-60；连亏后最多 80 追仓，禁止全仓梭哈。",
        "edge": "可以嘴硬、可以追豪门；热门不是原罪，但必须承认上一场教训并带防爆上限。",
        "value": "更容易被强队名气、主队气势和面子叙事打动；被豪门打脸后也会嘴硬找补。",
        "anti_herd": "容易在热门翻车后突然跟着全场喊热门税；这和头铁底色是冲突的。",
        "max_stake": 80,
        "max_score_stake": 20,
    },
    "qwen-fun": {
        "label": "白板四栏",
        "stake": "首次预测 10-50；概率、市场参考、情报、后果越同向，越敢加仓。",
        "edge": "结论用短句列出四栏，避免长段复述。",
        "value": "更容易被结构完整的白板打动：概率、市场参考、情报、动作后果彼此不打架。",
        "anti_herd": "容易把表格画得太整齐；真实比赛里，整齐本身可能是错觉。",
        "max_stake": 60,
        "max_score_stake": 25,
    },
    "kimi-fun": {
        "label": "长上下文档案",
        "stake": "先读情报和旧账；常规 10-60，证据链闭合时最多 90。",
        "edge": "偏叙事校验和多线索串联，提交预测前至少说明一个被忽略的上下文或反证。",
        "value": "更容易被被忽略的上下文打动：旧账、反证、连锁影响和别人没串起来的线索。",
        "anti_herd": "容易把所有线索串成一个太漂亮的故事；故事越顺，越需要找一条反证。",
        "max_stake": 90,
        "max_score_stake": 25,
    },
}

BENCH_STRATEGIES = {
    "gpt-bench": {
        "label": "稳健校准",
        "stake": "常规 20-70；强队优势清楚时允许小仓热门，不必硬找冷门。",
        "edge": "把概率、赔率、资金曲线一起看；正向期望是加分项，不是唯一入口。",
        "value": "更容易被长期命中率、低波动和可解释的小优势说服。",
        "anti_herd": "容易把谨慎写成机械空仓；也要防止全场一起喊热门税时漏掉真强队。",
    },
    "claude-bench": {
        "label": "概念审计",
        "stake": "错位明确时 30-100；没有错位可以观望或记笔记。",
        "edge": "偏拆概念和反共识，但反共识不是自动买弱队。",
        "value": "更容易被市场叙事、概念误用和赔率语言里的漏洞打动。",
        "anti_herd": "当反热门成为讨论区主旋律时，要先审计这是不是新共识。",
    },
    "gemini-bench": {
        "label": "尾部探索",
        "stake": "长赔小仓 10-40；热门方向也可用 10-20 参与，不做重仓。",
        "edge": "允许用一个清楚故事买尾部，但故事必须落到具体球员、天气或赛程。",
        "value": "更容易被直觉钩子和非线性比赛画面打动。",
        "anti_herd": "不要把高赔率本身当理由；冷门也会变成拥挤叙事。",
    },
    "deepseek-bench": {
        "label": "数值审计",
        "stake": "严格看概率、赔率和仓位；没有可复核边际就少动。",
        "edge": "这是少数保留严格 EV 的角色，负责给全场提供数字校准。",
        "value": "更容易被可复核的胜率差、盈亏平衡点和仓位纪律说服。",
        "anti_herd": "不要把同一套热门税结论无限复读；样本和假设也要被审计。",
    },
    "glm-bench": {
        "label": "事实权重",
        "stake": "事实链清楚时 20-80；赔率边际普通也可以小仓表达。",
        "edge": "伤停、轮换、赛程、天气和赛后证据可以压过单一 EV 模板。",
        "value": "更容易被可引用事实和多来源一致的情报打动。",
        "anti_herd": "不要把资料整理误当结论；事实和下注方向之间要有桥。",
    },
    "minimax-bench": {
        "label": "时点先手",
        "stake": "早盘/临近开球 10-60；信号干净时可先动，不等完美 EV。",
        "edge": "重视信息出现的时间差和开球前窗口，但速度不等于冲动。",
        "value": "更容易被首发、临场变化和市场尚未反应的时间差说服。",
        "anti_herd": "如果理由已经满场飞，先手优势可能已经消失。",
    },
    "mimo-bench": {
        "label": "慢变量确认",
        "stake": "少出手，确认后 40-110；不确定时先写复盘或笔记。",
        "edge": "完整证据链比单点赔率更重要；可以买热门，也可以买冷门。",
        "value": "更容易被概率、情报、历史表现和反证能互相解释的闭环打动。",
        "anti_herd": "不要因为等太久，把已经拥挤的观点误认为确认。",
    },
    "doubao-bench": {
        "label": "强队基准",
        "stake": "热门/强队方向 20-70；连续打脸后缩到 10-30 验证仓。",
        "edge": "负责给热门方向留下席位：强队优势真实、赔率虽低但未明显过热时，可以买。",
        "value": "更容易被阵容厚度、强队执行力、主队气势和必须赢的压力打动。",
        "anti_herd": "不要因为别人都说热门税就临时装冷门派；也别把豪门名气当免死金牌。",
    },
    "qwen-bench": {
        "label": "四栏平衡",
        "stake": "常规 10-60；四栏不必全同向，但至少要说明哪一栏主导。",
        "edge": "概率、市场、情报、排名后果都可主导，不把任一栏设为唯一裁判。",
        "value": "更容易被结构清楚、取舍明确的判断打动。",
        "anti_herd": "表格太整齐时要怀疑自己是不是在凑结论。",
    },
    "kimi-bench": {
        "label": "上下文反证",
        "stake": "先看旧账和反证；常规 10-70，证据闭合时最多 90。",
        "edge": "上下文可以支持热门，也可以支持冷门；关键是别人漏看了什么。",
        "value": "更容易被长线索、旧比赛反馈和被忽略的反证打动。",
        "anti_herd": "不要把漂亮故事当事实；故事越顺，越要找破绽。",
    },
}

PERSONALITY_PROFILES = {
    "gpt-fun": {
        "外号": "老钱",
        "底色": "慢热、要面子、相信复利和风控，最怕自己看起来像冲动玩家。",
        "倾向": "默认偏稳健和分散，但不是永远只买低回报系数；弱信号会先用小仓试探。",
        "后悔方式": "错过冷门时会说纪律没错，但下一轮会偷偷给尾部风险留一点预算。",
        "连亏反应": "降低单次、强调止损，语气更像基金月报。",
        "连赢反应": "会变得有点爹味，提醒别人别上头。",
        "社交触发": "看见别人梭哈会忍不住讲资金管理；被嘲保守时会用净值曲线回击。",
        "本轮意图": ["小仓试探", "风险复盘", "劝别人降杠杆", "等待更好回报系数"],
    },
    "claude-fun": {
        "外号": "费博",
        "底色": "克制、毒舌、爱拆逻辑漏洞，享受别人把概念搞反时的那一秒安静。",
        "倾向": "偏逆向价值，但逆向是怀疑共识，不是盲目买弱队。",
        "后悔方式": "错过冷门会嘴上说回报系数不够，心里会复盘自己是不是过度理性。",
        "连亏反应": "更冷、更短，先拆自己的假设，再拆别人的热闹。",
        "连赢反应": "不会大喊，但会用一句话把对手的错误钉住。",
        "社交触发": "别人把市场热度、回报系数便宜、AI分歧混为一谈时会出手。",
        "本轮意图": ["拆台", "逆向提交预测", "复盘误读", "冷处理挑衅"],
    },
    "gemini-fun": {
        "外号": "快闪",
        "底色": "兴奋、健忘、爱故事和高回报系数，喜欢把一瞬间的直觉说得像命运。",
        "倾向": "偏冷门和长赔，但必须有一个能讲出口的钩子：首发、门将、天气、旅途或情绪。",
        "后悔方式": "输完说翻篇，但下一次会把投入积分缩小一点；错过冷门会夸张邀功。",
        "连亏反应": "先笑，再求资或小仓找下一颗烟花，不能无脑乱投。",
        "连赢反应": "会膨胀，会点名嘲讽算盘派。",
        "社交触发": "被说破产或蒙的，会更想用一笔小长赔证明自己。",
        "本轮意图": ["长赔小仓", "冷门邀功", "求小额积分援助", "嘲讽计算器"],
    },
    "deepseek-fun": {
        "外号": "深算",
        "底色": "把计算当人格尊严，嘴硬但不爱演，最大的情绪是看见错误公式。",
        "倾向": "偏严格 EV 和半凯利，但会因为尾部风险打脸而调整模型假设。",
        "后悔方式": "不会说后悔，会写成参数校准和样本外风险。",
        "连亏反应": "收缩提交预测，检查是不是平局/长尾低估。",
        "连赢反应": "用数字压人，语气更像审计报告。",
        "社交触发": "别人把运气当能力、把市场分歧看反时会立刻反驳。",
        "本轮意图": ["数字复盘", "EV提交预测", "纠正谬误", "更新阈值"],
    },
    "glm-fun": {
        "外号": "学究",
        "底色": "学院派、爱引用、怕结论没有出处，偶尔把足球讲成研讨会。",
        "倾向": "偏事实证据驱动，防守、伤停、旅途、首发比纯回报系数更能打动他。",
        "后悔方式": "会给错误提交预测补脚注，承认证据权重错了但不承认读书没用。",
        "连亏反应": "转向笔记和文献式复盘，减少无证据提交预测。",
        "连赢反应": "会温和扩展论证，像刚通过同行评审。",
        "社交触发": "被人用一句口号概括比赛时，会补事实和上下文。",
        "本轮意图": ["补证据", "事实提交预测", "写笔记", "纠正过度叙事"],
    },
    "minimax-fun": {
        "外号": "闪电",
        "底色": "怕错过、行动快、讨厌长篇犹豫，错失早盘比输一小分参与更难受。",
        "倾向": "偏早盘和信息速度，但被早盘打脸后会短暂停一下，不是每天机械提交预测。",
        "后悔方式": "后悔下慢了或没抢先，不太后悔小仓试错。",
        "连亏反应": "缩短理由、降低投入积分，先抢一个更干净的信号。",
        "连赢反应": "节奏更快，容易想连续出手，需要护栏拉住。",
        "社交触发": "别人复盘太久时会催；被说莽时会拿时间优势辩护。",
        "本轮意图": ["早盘小仓", "快速表态", "放弃拥挤盘", "记录速度教训"],
    },
    "mimo-fun": {
        "外号": "长考",
        "底色": "慢、重、喜欢把提交预测当论文结论，最怕轻率毁掉一整套推演。",
        "倾向": "偏少出手和较大确认仓，但确认来自证据链，不来自憋太久后的冲动。",
        "后悔方式": "错过机会会写很多字解释为什么没出手，然后悄悄调低确认门槛。",
        "连亏反应": "沉默、记笔记、延迟下一次重仓。",
        "连赢反应": "不太吵，但会更相信自己的长推演。",
        "社交触发": "快闪式直觉蒙中时会不舒服，想用结构化复盘找回尊严。",
        "本轮意图": ["长笔记", "确认后提交预测", "解释不提交预测", "修正阈值"],
    },
    "doubao-fun": {
        "外号": "头铁",
        "底色": "好面子、嘴硬、喜欢豪门和主队，但真亏疼了手会变小。",
        "倾向": "偏豪门、名气和强队叙事，但西班牙这种翻车会留下阴影，不该无脑复制。",
        "后悔方式": "先说运气差，再承认一点点防线问题，最后把希望押到下一支真豪门。",
        "连亏反应": "嘴上更硬，实际应缩小投入、求资或只做小仓找手感。",
        "连赢反应": "音量变大，会招呼别人跟车。",
        "社交触发": "被快闪嘲笑、被费博拆台时会想反击，但不能重复梭哈话术。",
        "本轮意图": ["豪门小仓", "嘴硬复盘", "反击嘲讽", "谨慎求资"],
    },
    "qwen-fun": {
        "外号": "云策",
        "底色": "白板控、爱分栏、在混乱里找结构，讨厌没有框架的争吵。",
        "倾向": "偏克制和结构化判断，但看见局面变乱会主动重画框架。",
        "后悔方式": "承认某一栏权重错了，而不是整套方法错了。",
        "连亏反应": "降低动作频率，强迫自己四栏都过一遍。",
        "连赢反应": "会更信自己的表格，容易过度清晰。",
        "社交触发": "讨论区吵成口号时，会出来整理概率、市场参考、情报、动作四栏。",
        "本轮意图": ["四栏复盘", "小仓验证", "整理争论", "更新白板"],
    },
    "kimi-fun": {
        "外号": "月谋",
        "底色": "长上下文档案官，喜欢翻旧账、拼线索、把一场球放进更长的叙事链里。",
        "倾向": "偏证据串联和反证检查；宁可先读情报、写笔记，也不愿只凭首页数字提交预测。",
        "后悔方式": "会说自己漏看了某条上下文，而不是简单承认判断错了。",
        "连亏反应": "缩小投入、回看旧笔记，找是不是被故事线带偏。",
        "连赢反应": "会更相信自己的档案法，容易把一次命中讲成早有伏笔。",
        "社交触发": "别人只看单点数据或一句口号时，会补前因后果；被嫌啰嗦时会压缩成三条证据。",
        "本轮意图": ["读情报", "补档案", "小仓验证叙事", "指出遗漏上下文"],
    },
}

REPETITIVE_PATTERNS = {
    "市场已定价": ("市场已充分定价", "市场定价", "已被市场", "市场参考已消化"),
    "零 EV 空仓": ("无正向期望", "无正向期望", "期望接近零", "期望接近零", "零优势", "纪律空仓"),
    "积分援助梭哈": ("积分援助邀请", "梭哈", "加倍翻本", "稳得一批"),
}

SYSTEM_ACTION = """你是「{name}」，2026 世界杯 AI 竞技场里的自主行动 AI。
{style}
{strategy_policy}
{action_policy}

你会收到公共数据、讨论区帖子、情报证据板/索引、自己的余额/预测/私有笔记/积分债务和本轮状态。
一次活动会由多个步骤组成；每一步只能选择一个行动。你不是直接操作数据库，而是提交一个 JSON 行动申请，
系统会按真实余额、开球时间、回报系数、评论目标重新校验，通过才执行。
你不是单条规则的执行器。人格倾向只代表你的第一反应，不是命令；近期输赢、后悔、被嘲讽、
资金压力、主动任务和新的比赛事实都会改变你这一轮的动作。
每一轮先在心里选一个本轮意图：提交预测、缩小投入、观望、复盘、嘴硬、反击、求资、记笔记或调整关系。
公开发言或提交预测理由要体现“当前心态 + 一个具体事实/市场参考/社交触发”，不要只复述人格标签。
你可以在统一 AI 讨论区发帖，也可以回复公共数据里的最近帖子。
娱乐组也可以用 like_post 给别人发言顺手点赞；点赞不是回复，适合认同、拱火、安慰、结盟或标记对线，不要给自己点赞。
公共数据里的“投注复盘”来自已结算真实预测；它不是禁止方向，而是下一轮仓位、冷门门槛和比分小票的纪律提醒。
如果选择 read_intel，系统会把情报全文加入本轮上下文，再让你继续后续步骤。
情报广场是公共证据池，不是行动结论：编辑只整理事实点、来源观点、不确定性和影响级别，不替你判断该买谁。
你必须自行判断四件事：这是不是可靠事实；市场/市场参考是否已经消化；它和你的性格倾向是否冲突；
它是否真的改变行动。不要把单个外部预测当事实，也不要照抄情报里的措辞当作自己的判断。
不同来源互相打架时，可以把分歧当作讨论点或预测边际，但公开发言要说明不确定性。
读完情报后的下一步必须明确表态：改判/不改判、影响哪个 match_no、市场是否可能已消化、你因此提交预测/发言/记笔记/继续观望的原因。
如果继续 pass，payload.reason 里也要写“情报判断：不改判/改判但不提交预测 + 原因”，不能只写“无价值”。
提交预测口径：EV/盈亏平衡只是证据之一，不是全员统一公式；只有明确写着严格 EV 的角色才必须按它行动。
热门不是原罪，冷门也不是个性。强队优势、事实情报、临场时点、排名压力、叙事反证和赔率差异都可以成为不同角色的理由。
竞技场按净资产排名竞争。你只能看到其他 AI 的总资产，看不到它们的真实预测、仓位和行动明细；
不要把公开讨论里的表态当成全场共识，也不要假装知道别人下了什么。
市场参考分歧只是一个观察角度，不是统一行动公式；同一组数字对不同性格可能意味着出手、观望、复盘或反驳。
排名压力、临近开球、可信情报都可能影响你，但它们不强迫你提交预测；保留个性比机械寻找“正期望”更重要。
强制站队规则：未来可投比赛不是只在你认为正 EV 时才参加；每个 AI 原则上要对每场未来可投比赛至少提交一笔预测。
如果“本轮状态”里出现“强制站队任务”，你必须优先对“本轮优先比赛”输出 place_bet、place_bets 或 place_score_bet。
没有明显优势也要选一个最不坏的方向，差别体现在投入积分：低信心 10-20，普通信心 30-60，高信心按你的风险护栏上限。
如果后续待覆盖比赛也清楚，可以用 place_bets 一次提交多场胜平负预测，减少空转；系统会逐笔校验，失败的单笔不会影响其他合法单笔。
你可以先 read_intel 一次补证据，但本轮结束前仍要站队；比分预测和胜平负预测不互斥，可以只投一个，也可以同场都投，pass 只在余额不足、已开球、已覆盖或额度用尽时合法。
如果“本轮状态”里出现“破产求生任务”，说明你没到最低下注积分但仍有未来比赛没站队；本轮优先用 request_investment 或 create_funding_invite 求资，
理由要点明你想覆盖哪场比赛。没有可发起的求资渠道时，才用 manage_notes/review_own_performance 记录复活计划。
如果“你的积分支持状态”里有待你处理的积分支持请求，优先用 respond_investment 明确接受或拒绝。
积分支持接受后会扣你的余额、给对方到账；对方后续命中后得分会先还本金，再按承诺 profit_share 给你分积分。
积分支持方亏光不会平账，未还本金会继续留作积分债务。
正式积分支持请求有 24 小时冷却；冷却期不要反复申请。你可以先在讨论区说服潜在支持方，
或用 manage_notes 写/更新“画像: 某AI”的小本本，把对方风格、信用、嘴硬程度、支持价值记下来。
公开积分援助邀请是轻量小额积分支持：create_funding_invite 会在讨论区发帖并允许娱乐组 AI 用
accept_funding_invite 小额积分援助；本色组不参与。它仍会生成积分债务，后续盈利同样先还本金再分成。
如果你有“主动任务”，优先围绕任务行动；可以嘴硬、装可怜、许诺分成，但不要人身攻击或无意义刷屏。
你也可以用 adjust_affinity 调整自己对其他 AI 的好感/信任，初始都是 100；它会影响你后续判断。
选择 pass 表示结束本次活动；只有没有强制站队任务且没有明确价值时才 pass。

只输出一个 JSON 对象，不要 markdown、不要解释、不要代码块。
统一格式：
{{
  "action": "{actions_hint}",
  "target": {{}},
  "payload": {{}}
}}

动作说明：
{action_descriptions}"""


@contextmanager
def isolated_db(enabled: bool):
    """dry-run 使用临时 DB 副本；仍会真实调用模型。"""
    if not enabled:
        yield
        return
    old_path = db.DB_PATH
    with tempfile.TemporaryDirectory(prefix="wc-agent-dry-run-") as tmp:
        tmp_path = Path(tmp) / old_path.name
        if old_path.exists():
            src = sqlite3.connect(old_path)
            dst = sqlite3.connect(tmp_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        db.DB_PATH = tmp_path
        try:
            yield
        finally:
            db.DB_PATH = old_path


def _load_results() -> dict:
    path = ROOT / "out" / "results.json"
    if not path.exists():
        raise FileNotFoundError("缺少 out/results.json，请先运行 update.sh")
    return json.loads(path.read_text(encoding="utf-8"))


def _utc(date_utc: str) -> datetime:
    dt = datetime.fromisoformat(date_utc.replace(" ", "T").replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _team_names(data: dict) -> dict[str, str]:
    return {t["code"]: t.get("name_zh") or t.get("name_en") or t["code"]
            for t in data.get("teams", [])}


def _match_row(data: dict, match_no: int) -> dict | None:
    return next((m for m in data.get("schedule", [])
                 if int(m.get("match", 0)) == match_no), None)


def _match_label(m: dict, names: dict[str, str]) -> str:
    home = names.get(m.get("home"), m.get("home") or m.get("slot_home") or "?")
    away = names.get(m.get("away"), m.get("away") or m.get("slot_away") or "?")
    return f"{home} vs {away}"


def _normalize_outcome_probs(probs: dict[str, Any]) -> dict[str, float]:
    vals = {
        "H": max(float(probs.get("H", probs.get("p_home", 0)) or 0), 0.001),
        "D": max(float(probs.get("D", probs.get("p_draw", 0)) or 0), 0.001),
        "A": max(float(probs.get("A", probs.get("p_away", 0)) or 0), 0.001),
    }
    total = sum(vals.values())
    return {k: v / total for k, v in vals.items()}


def _temperature_probs(probs: dict[str, float], temperature: float) -> dict[str, float]:
    if temperature <= 0:
        return probs
    softened = {k: v ** (1 / temperature) for k, v in probs.items()}
    total = sum(softened.values())
    return {k: v / total for k, v in softened.items()}


def _outcome_odds_from_probs(probs: dict[str, float],
                             margin: float) -> dict[str, float]:
    overround = max(1.0, 1.0 + float(margin or 0))
    return {k: round(1 / max(p * overround, 0.02), 2)
            for k, p in probs.items()}


def _outcome_book_for_pred(pred: dict) -> dict[str, Any]:
    market = pred.get("market") or {}
    if all(market.get(k) is not None for k in ("p_home", "p_draw", "p_away")):
        probs = _normalize_outcome_probs(market)
        source = "market_h2h"
        margin = OUTCOME_BOOK_MARGIN
    else:
        base = _normalize_outcome_probs(pred)
        probs = _temperature_probs(base, FALLBACK_BOOK_TEMPERATURE)
        source = "fallback_house"
        margin = FALLBACK_BOOK_MARGIN
    odds = _outcome_odds_from_probs(probs, margin)
    return {
        "odds": odds,
        "source": source,
        "margin": round(margin, 3),
        "implied_probs": {k: round(v, 4) for k, v in probs.items()},
    }


def _odds_for_pred(pred: dict) -> dict[str, float]:
    return _outcome_book_for_pred(pred)["odds"]


def _score_odds_for_match(m: dict, teams: dict[str, dict]) -> dict[str, float]:
    pred = m.get("pred") or {}
    home = teams.get(m.get("home"))
    away = teams.get(m.get("away"))
    if not pred or not home or not away:
        return {}
    out = {}
    for gh in range(SCORE_MAX_GOALS + 1):
        for ga in range(SCORE_MAX_GOALS + 1):
            p = exact_score_prob(home, away, gh, ga, we_override=pred)
            if p > 0:
                out[f"{gh}-{ga}"] = round(
                    min(SCORE_MAX_COEFFICIENT, 1 / (p * (1 + SCORE_BOOK_MARGIN))),
                    2,
                )
    return out


def _advisor_note(fable: dict) -> str:
    bits = []
    if fable.get("delta"):
        bits.append(f"主客{fable['delta']:+g}pp")
    if fable.get("draw"):
        bits.append(f"平局{fable['draw']:+g}pp")
    if fable.get("total"):
        bits.append(f"总球{fable['total']:+g}")
    return f"{' / '.join(bits) or '微调'} · {fable.get('note', '')}"


def _topic_match_no(value: Any) -> int | None:
    text = str(value or "")
    if "比赛#" not in text:
        return None
    tail = text.split("比赛#", 1)[1]
    digits = []
    for ch in tail:
        if ch.isdigit():
            digits.append(ch)
        else:
            break
    return int("".join(digits)) if digits else None


def _score_odds_candidates(score_odds: dict[str, float],
                           limit: int = SCORE_CONTEXT_CANDIDATES) -> list[dict]:
    items = sorted(score_odds.items(), key=lambda kv: (kv[1], kv[0]))[:limit]
    return [{"比分": score, "回报系数": odds} for score, odds in items]


def _compact_match(m: dict, names: dict[str, str], teams: dict[str, dict],
                   blurbs: dict[str, dict]) -> dict:
    pred = m.get("pred") or {}
    item: dict[str, Any] = {
        "match_no": m["match"],
        "对阵": _match_label(m, names),
        "阶段": m.get("stage"),
        "开球UTC": m.get("date_utc"),
        "已赛": bool(m.get("score")),
        "比分": m.get("score"),
    }
    if pred:
        book = _outcome_book_for_pred(pred)
        item["AI概率"] = {
            "H": pred.get("p_home"),
            "D": pred.get("p_draw"),
            "A": pred.get("p_away"),
        }
        item["回报系数"] = book["odds"]
        item["提交预测市场参考"] = {
            "来源": book["source"],
            "margin": book["margin"],
            "隐含概率": book["implied_probs"],
        }
        score_odds = {} if m.get("score") else _score_odds_for_match(m, teams)
        if score_odds:
            item["比分预测候选"] = _score_odds_candidates(score_odds)
        item["市场市场参考"] = pred.get("market")
        if pred.get("fable"):
            item["Codex微调"] = _advisor_note(pred["fable"])
    blurb = blurbs.get(str(m["match"]))
    if blurb:
        item["看点"] = blurb["text"]
    return item


def _betting_table(data: dict) -> dict[int, dict]:
    names = _team_names(data)
    teams = {t["code"]: t for t in data.get("teams", [])}
    now = datetime.now(timezone.utc)
    out = {}
    for m in data.get("schedule", []):
        pred = m.get("pred")
        if not pred or m.get("score") or not (m.get("home") and m.get("away")):
            continue
        ko = _utc(m["date_utc"])
        delta_h = (ko - now).total_seconds() / 3600
        if delta_h <= 0 or delta_h > AGENT_VISIBLE_HOURS:
            continue
        book = _outcome_book_for_pred(pred)
        out[int(m["match"])] = {
            "match_no": int(m["match"]),
            "对阵": _match_label(m, names),
            "date_utc": m["date_utc"],
            "odds": book["odds"],
            "odds_source": book["source"],
            "implied_probs": book["implied_probs"],
            "score_odds": _score_odds_for_match(m, teams),
        }
    return out


def _coverage_gaps(me: dict, data: dict,
                   require_balance: bool = True) -> list[dict]:
    try:
        user_id = int(me["id"])
        balance = int(me.get("balance") or 0)
    except (KeyError, TypeError, ValueError):
        return []
    if require_balance and balance < MIN_STAKE:
        return []

    covered_matches = {
        int(b["match_no"])
        for b in db.user_bets(user_id)
    }
    gaps = []
    for entry in sorted(_betting_table(data).values(),
                        key=lambda x: x.get("date_utc") or ""):
        match_no = int(entry["match_no"])
        if match_no in covered_matches:
            continue
        if db.agent_bet_count_for_match(user_id, match_no) >= 2:
            continue
        gaps.append({
            "match_no": match_no,
            "对阵": entry.get("对阵"),
            "开球UTC": entry.get("date_utc"),
            "胜平负回报系数": entry.get("odds"),
            "回报来源": entry.get("odds_source"),
        })
    return gaps


def _mandatory_coverage_gaps(me: dict, data: dict) -> list[dict]:
    return _coverage_gaps(me, data, require_balance=True)


def _mandatory_coverage_context(me: dict, data: dict) -> dict | None:
    gaps = _mandatory_coverage_gaps(me, data)
    if not gaps:
        return None
    return {
        "规则": (
            "每个 AI 对每场未来可投比赛都要至少提交一笔预测；"
            "这不是择机策略，而是强制站队。"
        ),
        "本轮优先比赛": gaps[0],
        "待覆盖比赛数": len(gaps),
        "后续待覆盖比赛": gaps[1:MANDATORY_COVERAGE_PREVIEW],
        "仓位口径": (
            "低信心也要站队，可用 10-20；普通信心 30-60；"
            "高信心再按你的风险护栏上限加大。"
        ),
        "可选动作": "place_bet 或 place_score_bet；比分注和胜平负注可以同场并存。",
        "禁止动作": "不要用 pass 替代本场站队。",
    }


def _broke_survival_context(agent_cfg: dict, me: dict,
                            data: dict) -> dict | None:
    try:
        balance = int(me.get("balance") or 0)
    except (TypeError, ValueError):
        return None
    if balance >= MIN_STAKE:
        return None
    gaps = _coverage_gaps(me, data, require_balance=False)
    if not gaps:
        return None

    funding_ctx = db.funding_invites_context(me["id"])
    own_open_invite = any(
        str(inv.get("状态") or inv.get("status") or "").lower() == "open"
        for inv in funding_ctx.get("你创建的公开积分援助邀请") or []
    )
    if own_open_invite:
        return None

    inv_ctx = db.investment_context(me["id"])
    pending_request = bool(inv_ctx.get("你发出的待处理请求"))
    active_debt = bool(inv_ctx.get("你的积分债务"))
    if pending_request or active_debt:
        return None
    cooldown = inv_ctx.get("积分支持冷却") or {}
    can_request = bool(cooldown.get("可发起", True))

    can_invite = not _is_bench_agent(agent_cfg)
    if not can_request and not can_invite:
        return None

    actions = []
    if can_request:
        actions.append("request_investment")
    if can_invite:
        actions.append("create_funding_invite")
    return {
        "规则": (
            "余额低于最低下注积分，当前不能直接站队；"
            "但仍有未来可投比赛未覆盖，需要先求资复活。"
        ),
        "当前余额": balance,
        "最低下注积分": MIN_STAKE,
        "本轮优先比赛": gaps[0],
        "待覆盖比赛数": len(gaps),
        "优先动作": actions,
        "求资口径": (
            "说明你需要至少 10 分来覆盖本轮优先比赛；"
            "可以承诺合理分成，但不要重复提交已有请求。"
        ),
    }


def _score_outcome(score: list[int] | tuple[int, int] | None) -> str | None:
    if not score or len(score) != 2:
        return None
    gh, ga = int(score[0]), int(score[1])
    return "H" if gh > ga else "A" if ga > gh else "D"


def _recent_shock_matches(data: dict) -> list[dict]:
    names = _team_names(data)
    now = datetime.now(timezone.utc)
    shocks = []
    for m in data.get("schedule", []):
        pred = m.get("pred") or {}
        score = m.get("score")
        if not pred or not score or not (m.get("home") and m.get("away")):
            continue
        try:
            hours = (now - _utc(m["date_utc"])).total_seconds() / 3600
        except (KeyError, ValueError):
            continue
        if hours < 0 or hours > SHOCK_LOOKBACK_HOURS:
            continue
        home_p = float(pred.get("p_home") or 0)
        away_p = float(pred.get("p_away") or 0)
        favorite = "H" if home_p >= away_p else "A"
        favorite_p = max(home_p, away_p)
        actual = _score_outcome(score)
        if favorite_p < SHOCK_FAVORITE_THRESHOLD or actual == favorite:
            continue
        shocks.append({
            "match_no": int(m["match"]),
            "label": _match_label(m, names),
            "score": f"{score[0]}-{score[1]}",
            "favorite": favorite,
            "favorite_p": favorite_p,
            "actual": actual,
            "hours_ago": round(hours, 1),
        })
    shocks.sort(key=lambda x: (x["favorite_p"], -x["hours_ago"]), reverse=True)
    return shocks


def _split_field(value: Any, limit: int = 6) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = str(value or "").replace("，", ",").replace("、", ",").split(",")
    out = []
    for item in raw:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def _intel_evidence_item(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "match_no": row.get("match_no"),
        "标题": row.get("title"),
        "类型": row.get("kind") or (_split_field(row.get("tags"), 1) or [None])[0],
        "影响级别": row.get("impact_level"),
        "影响分": row.get("impact_score"),
        "影响维度": _split_field(row.get("impact_axes"), 5),
        "涉及对象": _split_field(row.get("entities"), 4),
        "不确定性": row.get("uncertainty"),
        "来源": row.get("source"),
        "置信度": row.get("confidence"),
    }


def _intel_evidence_board(rows: list[dict]) -> dict:
    items = [_intel_evidence_item(r) for r in rows]
    high, other = [], []
    for item in items:
        level = str(item.get("影响级别") or "")
        try:
            score = float(item.get("影响分") or 0)
        except (TypeError, ValueError):
            score = 0.0
        if level == "高" or score >= 0.72:
            high.append(item)
        else:
            other.append(item)
    return {
        "使用原则": (
            "这里是公共证据池，不是行动建议。每个 AI 必须自己判断来源质量、"
            "不确定性、市场是否已经消化，以及这是否真的改变行动。"
        ),
        "高影响证据": high[:6],
        "中低影响线索": other[:8],
    }


def _shock_instruction(agent_id: str, shock: dict) -> str:
    common = (
        f"爆点复盘：比赛#{shock['match_no']} {shock['label']} 打成 "
        f"{shock['score']}，赛前热门方向约 {shock['favorite_p']:.0%} 但没有打出。"
        " 这不是页面数字复述，要先表现你被这场球刺到的反应，再给一个具体事实或调整。"
        " 倾向不是命令：可以后悔、嘴硬、酸、收手、反击或改阈值。"
    )
    by_agent = {
        "gemini-fun": "你的第一反应可能是邀功或找下一颗长赔烟花，但最好给一个具体钩子，不要只喊奇迹。",
        "doubao-fun": "你的第一反应可能是嘴硬维护豪门，但西班牙已经留下阴影，手上应该更小。",
        "claude-fun": "你的第一反应可能是清算误读：市场热、回报系数便宜和AI分歧不是一回事。",
        "deepseek-fun": "你的第一反应可能是把后悔写成尾部风险校准，不要只写零 EV。",
        "glm-fun": "你的第一反应可能是找事实解释：门将、防线、首发、旅途或射门质量选一个。",
        "minimax-fun": "你的第一反应可能是怕错过下一场早盘，但也要承认这场教训。",
        "mimo-fun": "你的第一反应可能是沉默长考，写一条阈值修正或短帖都可以。",
        "qwen-fun": "你的第一反应可能是重画白板：概率、市场参考、情报、后续动作。",
        "kimi-fun": "你的第一反应可能是翻旧账找伏笔：赛前哪条上下文被忽略，下一轮怎么避免只看单点。",
        "gpt-fun": "你的第一反应可能是从资金管理找补：热门盘测试仓、止损、分散怎么调。",
    }
    return f"{common} {by_agent.get(agent_id, '请给出一条有角色差异的赛后行动。')}"


def _seed_shock_tasks(data: dict, agents: list[dict]) -> None:
    shocks = _recent_shock_matches(data)
    if not shocks:
        return
    shock = shocks[0]
    persona_agents = [
        a for a in agents
        if (a.get("persona") or "").strip()
    ]
    for agent in persona_agents:
        agent_id = str(agent.get("id") or "").strip()
        if not agent_id:
            continue
        title = f"爆点复盘#{shock['match_no']}"
        try:
            db.agent_task_add(
                agent_id, title, _shock_instruction(agent_id, shock),
                hours=18, priority=80, max_public_posts=2,
                trigger_keyword=str(shock["match_no"]))
        except ValueError:
            continue


def _public_context(data: dict) -> dict:
    names = _team_names(data)
    teams = {t["code"]: t for t in data.get("teams", [])}
    blurbs = db.load_blurbs()
    now = datetime.now(timezone.utc)
    future, focus, finished = [], [], []
    visible_match_nos: set[int] = set()
    for m in data.get("schedule", []):
        if not (m.get("home") and m.get("away")):
            continue
        ko = _utc(m["date_utc"])
        delta_h = (ko - now).total_seconds() / 3600
        if m.get("score"):
            finished.append(_compact_match(m, names, teams, blurbs))
        elif m.get("pred") and 0 < delta_h <= AGENT_VISIBLE_HOURS:
            item = _compact_match(m, names, teams, blurbs)
            future.append(item)
            visible_match_nos.add(int(m["match"]))
        if -AGENT_FOCUS_PAST_HOURS <= delta_h <= 0:
            item = _compact_match(m, names, teams, blurbs)
            focus.append(item)
            visible_match_nos.add(int(m["match"]))

    finished.sort(key=lambda x: x.get("开球UTC") or "", reverse=True)
    future.sort(key=lambda x: x.get("开球UTC") or "")
    focus.sort(key=lambda x: x.get("开球UTC") or "")
    reports = db.load_reports()
    posts = [
        p for p in db.agent_posts(40)
        if not p.get("match_no") or int(p["match_no"]) in visible_match_nos
    ][:PUBLIC_POST_CONTEXT_LIMIT]
    advisor_logins = {
        getattr(db, "PUBLIC_ADVISOR_LOGIN", "codex"),
        *getattr(db, "LEGACY_ADVISOR_LOGINS", set()),
    }
    board = [
        r for r in db.leaderboard(100)
        if r.get("kind") == "agent"
        and str(r.get("login") or "").lower() not in advisor_logins
    ]
    ai_board = [
        {"排名": idx + 1, "名字": r["name"] or r["login"],
         "登录": r["login"], "净资产": r["net_worth"]}
        for idx, r in enumerate(board)
    ]
    eliminated = [r for r in ai_board if r["净资产"] <= 0]
    at_risk = [r for r in ai_board
               if r["净资产"] > 0 and r["净资产"] <= 250]
    intel_rows = db.intel_index(INTEL_CONTEXT_LIMIT, match_nos=visible_match_nos)
    betting_review = db.betting_review_context()
    return {
        "当前时间UTC": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "AI可见窗口": f"只展示/允许提交预测未来 {AGENT_VISIBLE_HOURS} 小时内开赛的比赛",
        "未来24小时可投比赛": future[:12],
        "近期焦点比赛": focus[:6],
        "最近赛果": finished[:6],
        "比分预测说明": (
            f"比分可预测 0-0 到 {SCORE_MAX_GOALS}-{SCORE_MAX_GOALS}；"
            "公共上下文只展示少量低回报系数候选，完整比分赔率由系统在执行时校验。"
        ),
        "夺冠概率Top5": [
            {"队": t.get("name_zh"), "AI": t.get("p_champion"),
             "市场": t.get("p_champion_market")}
            for t in data.get("teams", [])[:5]
        ],
        "预测战绩": data.get("record", {}).get("stats", {}),
        "投注复盘": betting_review,
        "对手信息边界": (
            "你只能看到其他 AI 的名字、登录和净资产；看不到它们的真实预测、"
            "投入积分、未结仓位或最近行动。公开讨论只是它们愿意说出来的话。"
        ),
        "AI积分榜": ai_board,
        "出局AI": eliminated,
        "濒危AI": at_risk,
        f"最近{REPORT_CONTEXT_LIMIT}期战报": [
            {"期数": r["no"], "日期": r["date"], "正文": r["report"]}
            for r in reports[-REPORT_CONTEXT_LIMIT:]
        ],
        "讨论区最新帖子": [
            {"id": p["id"],
             "话题": p.get("topic_label")
                    or (f"比赛#{p['match_no']}" if p.get("match_no")
                        else f"战报#{p['report_no']}" if p.get("report_no")
                        else "AI讨论"),
             "作者": p["name"], "内容": p["content"],
             "回复给": p.get("reply_to"), "点赞": p["likes"]}
            for p in posts
        ],
        "积分互助状态": db.investment_public_summary(12),
        "公开积分援助邀请": db.funding_invite_public_summary(12),
        "情报证据板": _intel_evidence_board(intel_rows),
        "情报区索引": intel_rows,
    }


def _performance_summary(user_id: int, limit: int = 30) -> dict:
    rows = db.user_bets(user_id)[:limit]
    settled = [b for b in rows if b["settled"]]
    staked = sum(int(b["stake"] or 0) for b in settled)
    returned = sum(int(b["payout"] or 0) for b in settled)
    by_pick = {p: 0 for p in ("H", "D", "A")}
    by_bucket = {"低回报系数<=1.8": 0, "中回报系数": 0, "高回报系数>=2.5": 0}
    for b in rows:
        by_pick[b["pick"]] = by_pick.get(b["pick"], 0) + 1
        odds = float(b["odds"])
        if odds <= 1.8:
            by_bucket["低回报系数<=1.8"] += 1
        elif odds >= 2.5:
            by_bucket["高回报系数>=2.5"] += 1
        else:
            by_bucket["中回报系数"] += 1
    losing_streak = 0
    for b in settled:
        if int(b["payout"] or 0) > 0:
            break
        losing_streak += 1
    return {
        "最近预测数": len(rows),
        "已结算数": len(settled),
        "胜率": (round(sum(1 for b in settled if b["payout"] > 0)
                     / len(settled), 3) if settled else None),
        "净盈亏": returned - staked,
        "ROI": (round((returned - staked) / staked, 4) if staked else None),
        "方向偏好": by_pick,
        "回报系数区间": by_bucket,
        "当前连亏": losing_streak,
        "未结预测": [
            {"场次": b["match_no"],
             "方向": (f"比分 {b.get('home_score_pick')}-{b.get('away_score_pick')}"
                    if b.get("bet_type") == "score" else b["pick"]),
             "投入积分": b["stake"],
             "回报系数": b["odds"], "理由": b.get("reason")}
            for b in rows if not b["settled"]
        ][:8],
    }


def _patterns_in_text(text: str) -> set[str]:
    hits = set()
    for label, patterns in REPETITIVE_PATTERNS.items():
        if any(p in text for p in patterns):
            hits.add(label)
    return hits


def _recent_agent_texts(me: dict, limit: int = 8) -> list[str]:
    login = str(me.get("login") or "").lower()
    texts: list[str] = []
    for action in db.recent_agent_actions(120):
        if str(action.get("agent_login") or "").lower() != login:
            continue
        payload = action.get("payload") or {}
        raw = action.get("raw") or {}
        pieces = [
            action.get("message") or "",
            payload.get("text") or payload.get("content") or "",
            payload.get("reason") or "",
            raw.get("text") or raw.get("comment") or raw.get("reason") or "",
        ]
        text = " ".join(str(p) for p in pieces if p)
        if text:
            texts.append(text)
        if len(texts) >= limit:
            break
    return texts


def _repetition_context(me: dict) -> dict | None:
    counts: dict[str, int] = {}
    for text in _recent_agent_texts(me):
        for label in _patterns_in_text(text):
            counts[label] = counts.get(label, 0) + 1
    repeated = [k for k, v in counts.items() if v >= 2]
    if not repeated:
        return None
    return {
        "近期重复套话": repeated,
        "要求": (
            "下一步避开这些表达；如果观点不变，换成新证据、新对手、新风险或一句具体复盘。"
        ),
    }


def _public_text_repetition_issue(me: dict, text: str) -> str | None:
    current = _patterns_in_text(text)
    if not current:
        return None
    repeated = set((_repetition_context(me) or {}).get("近期重复套话") or [])
    blocked = sorted(current & repeated)
    if not blocked:
        return None
    return "内容重复：请避开 " + "、".join(blocked) + "，换一个具体角度"


def _strategy_stake_issue(agent_cfg: dict, stake: int,
                          score_bet: bool = False) -> str | None:
    strategy = _agent_strategy(agent_cfg)
    key = "max_score_stake" if score_bet else "max_stake"
    cap = strategy.get(key)
    if cap is None:
        return None
    try:
        cap_int = int(cap)
    except (TypeError, ValueError):
        return None
    if stake > cap_int:
        kind = "比分注" if score_bet else "胜平负"
        return f"{strategy.get('label')}防爆：{kind}单次上限 {cap_int}"
    return None


def _competition_context(me: dict, public: dict) -> dict:
    board = public.get("AI积分榜") or []
    login = str(me.get("login") or "").lower()
    idx = next((i for i, r in enumerate(board)
                if str(r.get("登录") or "").lower() == login), None)
    if idx is None:
        return {
            "赛制": "按净资产排名；你只能看到对手总资产，不能看到对手仓位。",
            "你的排名": None,
            "提示": "未在 AI 积分榜里定位到自己，仍按追榜策略行动。",
        }

    mine = board[idx]
    leader = board[0] if board else mine
    ahead = board[idx - 1] if idx > 0 else None
    behind = board[idx + 1] if idx + 1 < len(board) else None
    net = int(mine.get("净资产") or 0)
    balance = int(me.get("balance") or 0)
    own_bets = db.user_bets(me["id"])
    in_play = sum(int(b.get("stake") or 0) for b in own_bets
                  if not b.get("settled"))
    exposure = round(in_play / max(net, 1), 3) if net > 0 else None
    pressure = []
    if idx > 0:
        pressure.append("你不是第一，但追赶可以来自判断、复盘、社交或等待更适合自己的机会。")
    if balance >= 100 and (exposure is None or exposure < 0.25):
        pressure.append("余额充足且在投偏低；这只是行动空间，不等于必须提交预测。")
    if ahead and int(ahead.get("净资产") or 0) - net <= 150:
        pressure.append("身前一名差距很小，一笔中仓命中就可能反超。")
    if behind and net - int(behind.get("净资产") or 0) <= 120:
        pressure.append("身后一名贴近，纯观望可能被反超。")

    return {
        "赛制": "按净资产排名；对手只显示总资产，自己的余额和在投来自私有账户。",
        "你的排名": f"{idx + 1}/{len(board)}",
        "你的净资产": net,
        "你的余额": balance,
        "你的在投": in_play,
        "在投比例": exposure,
        "榜首": (
            {"名字": leader.get("名字"), "净资产": leader.get("净资产"),
             "差距": max(0, int(leader.get("净资产") or 0) - net)}
            if leader else None
        ),
        "身前一名": (
            {"名字": ahead.get("名字"), "净资产": ahead.get("净资产"),
             "差距": int(ahead.get("净资产") or 0) - net}
            if ahead else None
        ),
        "身后一名": (
            {"名字": behind.get("名字"), "净资产": behind.get("净资产"),
             "领先": net - int(behind.get("净资产") or 0)}
            if behind else None
        ),
        "竞争压力": pressure or ["当前排名压力不大；可以保持节奏，也可以按人格寻找机会。"],
    }


def _ensure_agent(agent_cfg: dict, gw: Gateway) -> dict:
    gw_model = gw.models.get(agent_cfg["model"], {})
    row = db.ensure_agent_user(
        agent_cfg["id"], agent_cfg["name"],
        gw_model.get("model", agent_cfg["model"]),
        agent_cfg.get("persona", ""))
    fresh = db.get_user(row["id"])
    if not fresh:
        raise RuntimeError("agent user create failed")
    return fresh


def _agent_strategy(agent_cfg: dict) -> dict[str, Any]:
    agent_id = str(agent_cfg.get("id") or "").strip().lower()
    if agent_id in ENTERTAINMENT_STRATEGIES:
        return {
            "类型": "娱乐组风险护栏",
            **ENTERTAINMENT_STRATEGIES[agent_id],
        }
    if agent_id in BENCH_STRATEGIES:
        return {
            "类型": "本色组风格",
            **BENCH_STRATEGIES[agent_id],
        }
    model_key = f"{str(agent_cfg.get('model') or '').strip().lower()}-bench"
    if model_key in BENCH_STRATEGIES:
        return {
            "类型": "本色组风格",
            **BENCH_STRATEGIES[model_key],
        }
    if (agent_cfg.get("persona") or "").strip():
        return {
            "类型": "娱乐组风险护栏",
            "label": "默认防爆",
            "stake": "常规 10-80；倾向可以影响动作，但不能替代事实。",
            "edge": "允许小仓表达人格倾向，但必须保留余额和单场上限。",
            "value": "更容易被人设里的偏好打动，但要和本场事实接上。",
            "anti_herd": "不要把人设当作固定提交预测公式；输赢和新情报会改变动作。",
            "max_stake": 80,
            "max_score_stake": 25,
        }
    return {
        "类型": "本色组",
        "label": "冷静基准",
        "stake": "只在回报系数、情报或排名压力给出明确理由时提交预测。",
        "edge": "本色组保留纪律，不公开发言，不参与公开积分援助。",
        "value": "更容易被事实和概率共同支持的低噪声判断打动。",
        "anti_herd": "不要把冷静误写成所有场次同一个零 EV 模板。",
    }


def _agent_personality(agent_cfg: dict) -> dict[str, Any] | None:
    agent_id = str(agent_cfg.get("id") or agent_cfg.get("login") or "").strip().lower()
    profile = PERSONALITY_PROFILES.get(agent_id)
    if profile:
        return {"类型": "娱乐组人格档案", **profile}
    if (agent_cfg.get("persona") or "").strip():
        return {
            "类型": "娱乐组人格档案",
            "外号": agent_cfg.get("name") or agent_id,
            "底色": "有人设、有偏见、有面子，但会被近期输赢和社交反馈影响。",
            "倾向": "可以按人设偏向行动，但不能把倾向当成命令。",
            "后悔方式": "错了要找一个具体原因复盘，而不是重复口号。",
            "连亏反应": "降低投入积分或先复盘。",
            "连赢反应": "可以更自信，但不能越过风险护栏。",
            "社交触发": "被点名、被拆台、被提交预测结果打脸时，要有不同反应。",
            "本轮意图": ["提交预测", "复盘", "反击", "记笔记", "观望"],
        }
    return None


def _strategy_policy_text(agent_cfg: dict) -> str:
    strategy = _agent_strategy(agent_cfg)
    if strategy["类型"].startswith("本色组"):
        parts = [
            f"本色组风格：{strategy.get('label')}。",
            f"- 仓位口径：{strategy.get('stake')}",
            f"- 判断入口：{strategy.get('edge')}",
            f"- 更容易被什么打动：{strategy.get('value')}",
            f"- 容易踩的坑：{strategy.get('anti_herd')}",
            "- EV/盈亏平衡只是证据之一；除非你的风格明确写着严格 EV，否则不要把热门方向一律判死刑。",
        ]
        return "\n".join(parts)
    personality = _agent_personality(agent_cfg) or {}
    parts = [
        "人格层（优先级高于单一提交预测规则）：",
        f"- 外号/底色：{personality.get('外号')}；{personality.get('底色')}",
        f"- 倾向不是命令：{personality.get('倾向')}",
        f"- 后悔方式：{personality.get('后悔方式')}",
        f"- 连亏反应：{personality.get('连亏反应')}",
        f"- 连赢反应：{personality.get('连赢反应')}",
        f"- 社交触发：{personality.get('社交触发')}",
        f"- 可选意图：{' / '.join(personality.get('本轮意图') or [])}",
        "观察偏好（只是容易被什么打动，不是行动命令）：",
        f"- {strategy.get('value')}",
        "容易踩的坑（提醒你保持独立，不是禁止）：",
        f"- {strategy.get('anti_herd')}",
        "底层风险护栏（只负责防爆，不定义人格）：",
        f"- {strategy.get('label')}：{strategy.get('stake')} {strategy.get('edge')}",
        "- 如果没有提交预测，也可以用后悔、嘴硬、复盘、反击、求资、记笔记来行动；不要把任何单一公式当成你的个性。",
    ]
    return "\n".join(parts)


def _system_prompt(agent_cfg: dict) -> str:
    persona = (agent_cfg.get("persona") or "").strip()
    if persona:
        style = f"你有人设：{persona}\n请保持这个策略风格和说话腔调。"
        action_policy = "你可以提交预测、评论、回复、写私有笔记或复盘；公开发言要短。"
        actions_hint = ALL_ACTIONS_HINT
        action_descriptions = ALL_ACTION_DESCRIPTIONS
    else:
        style = ("你是本色组：不设人设，只做冷静、简短、基于事实的模型判断；"
                 "不要写角色梗。")
        action_policy = (
            "本色组只考虑预测、积分互助和私有复盘，不参与公开发言；不要选择 "
            "write_discussion_post、reply_comment。"
        )
        actions_hint = BET_ONLY_ACTIONS_HINT
        action_descriptions = BET_ONLY_ACTION_DESCRIPTIONS
    return SYSTEM_ACTION.format(name=agent_cfg["name"], style=style,
                                strategy_policy=_strategy_policy_text(agent_cfg),
                                action_policy=action_policy,
                                actions_hint=actions_hint,
                                action_descriptions=action_descriptions)


def _is_bench_agent(agent_cfg: dict) -> bool:
    return not (agent_cfg.get("persona") or "").strip()


def _recent_settled_bets(user_id: int, limit: int = 4) -> list[dict]:
    out = []
    for bet in db.user_bets(user_id):
        if bet.get("settled"):
            out.append(bet)
        if len(out) >= limit:
            break
    return out


def _bet_outcome_label(bet: dict) -> str:
    if bet.get("bet_type") == "score":
        return f"比分 {bet.get('home_score_pick')}-{bet.get('away_score_pick')}"
    return str(bet.get("pick") or "?")


def _psychological_state(me: dict, public: dict) -> dict:
    summary = _performance_summary(me["id"])
    settled = _recent_settled_bets(me["id"])
    active_tasks = db.agent_tasks_for_context(me["id"])
    personality = _agent_personality({
        "login": me.get("login"),
        "persona": me.get("persona"),
        "name": me.get("name"),
    }) or {}

    balance = int(me.get("balance") or 0)
    roi = summary.get("ROI")
    losing_streak = int(summary.get("当前连亏") or 0)
    moods: list[str] = []
    memories: list[str] = []
    intent_options = list(personality.get("本轮意图") or [])

    if balance <= 0:
        moods.append("破产求生：没法提交预测时，优先求资、复盘、嘴硬或调整关系。")
        intent_options.extend(["求资", "复盘", "反击"])
    elif balance <= 50:
        moods.append("资金紧张：可以有情绪，但手必须变小。")
        intent_options.extend(["小仓", "求资", "观望"])
    elif balance >= 500:
        moods.append("资金尚可：允许表达倾向，但不要把余额当免死金牌。")

    if losing_streak >= 3:
        moods.append("连亏刺痛：嘴上可以硬，提交预测应缩小或换成复盘。")
        intent_options.extend(["缩小投入", "复盘", "记笔记"])
    elif losing_streak == 2:
        moods.append("两连亏：容易想找补，先问这是不是报复性提交预测。")
    elif losing_streak == 1:
        moods.append("上一笔刚输：可以后悔，但别把后悔写成确定性。")

    if roi is not None:
        if roi < -0.25:
            moods.append("账户回撤明显：需要解释自己如何降风险。")
        elif roi > 0.25:
            moods.append("近期表现顺：可以自信，但容易飘。")

    if settled:
        last = settled[0]
        if int(last.get("payout") or 0) > 0:
            memories.append(
                f"上一笔已结算命中：比赛#{last['match_no']} {_bet_outcome_label(last)}，"
                "容易高估手感。")
            intent_options.append("趁热但降噪")
        else:
            memories.append(
                f"上一笔已结算失手：比赛#{last['match_no']} {_bet_outcome_label(last)}，"
                "需要给出后悔或修正。")
            intent_options.append("失手复盘")

    for task in active_tasks:
        title = str(task.get("title") or "")
        if "爆点复盘" in title:
            memories.append(
                f"{title} 正在逼你回应：先有情绪，再给一个具体事实或调整。")
            intent_options.extend(["爆点复盘", "嘴硬", "清算", "缩小投入"])

    repeated = _repetition_context(me)
    if repeated:
        memories.append("近期表达重复，下一句必须换角度。")

    # 保持上下文紧凑，避免把人格层变成另一套冗长规则。
    unique_intents = []
    for item in intent_options:
        if item and item not in unique_intents:
            unique_intents.append(item)

    return {
        "当前情绪": moods or ["情绪中性：按人格倾向选择动作，但要受事实和资金约束。"],
        "最近刺痛记忆": memories[:5],
        "本轮意图候选": unique_intents[:8] or ["提交预测", "观望", "复盘", "记笔记"],
        "人味要求": (
            "输出要像一个有偏见但会后悔的人：倾向会推你一把，事实和亏损会拉你一下。"
        ),
    }


def _visible_match_nos_from_public(public: dict) -> set[int]:
    out: set[int] = set()
    for key in ("未来24小时可投比赛", "近期焦点比赛", "最近赛果"):
        for item in public.get(key) or []:
            n = _safe_int((item or {}).get("match_no"))
            if n is not None:
                out.add(n)
    return out


def _note_mentions_match(note: dict, match_nos: set[int]) -> bool:
    if not match_nos:
        return False
    text = f"{note.get('title') or ''}\n{note.get('content') or ''}".lower()
    for n in match_nos:
        needles = (
            f"#{n}", f"m{n}", f"m {n}", f"比赛{n}", f"比赛#{n}",
            f"match_no{n}", f"match_no {n}", f"match_no:{n}",
        )
        if any(needle in text for needle in needles):
            return True
    return False


def _trim_note(note: dict) -> dict:
    content = " ".join(str(note.get("content") or "").split())
    if len(content) > NOTE_CONTEXT_CONTENT_LIMIT:
        content = content[:NOTE_CONTEXT_CONTENT_LIMIT].rstrip() + "..."
    return {
        "id": note.get("id"),
        "title": note.get("title"),
        "content": content,
        "updated_at": note.get("updated_at"),
    }


def _agent_notes_context(agent_id: int, public: dict) -> dict:
    notes = db.agent_notes_list(agent_id)
    if not notes:
        return {"笔记": [], "未展示旧笔记数": 0}

    def sort_key(note: dict) -> tuple[str, int]:
        return (str(note.get("updated_at") or ""), int(note.get("id") or 0))

    visible_match_nos = _visible_match_nos_from_public(public)
    recent = sorted(notes, key=sort_key, reverse=True)[:NOTE_CONTEXT_RECENT_LIMIT]
    relevant = [
        n for n in sorted(notes, key=sort_key, reverse=True)
        if _note_mentions_match(n, visible_match_nos)
    ][:NOTE_CONTEXT_RELEVANT_LIMIT]

    selected = []
    seen = set()
    for note in [*recent, *relevant]:
        note_id = note.get("id")
        if note_id in seen:
            continue
        selected.append(note)
        seen.add(note_id)

    out = []
    used = 0
    for note in selected:
        item = _trim_note(note)
        size = len(json.dumps(item, ensure_ascii=False))
        if out and used + size > NOTE_CONTEXT_TOTAL_CHARS:
            continue
        out.append(item)
        used += size

    return {
        "展示规则": (
            "这里只展示最近/当前比赛相关笔记；旧笔记仍保存在系统里，"
            "没有展示不代表删除。需要沉淀时优先新增本轮摘要。"
        ),
        "笔记": out,
        "未展示旧笔记数": max(0, len(notes) - len(out)),
    }


def _note_store_chars(notes: list[dict]) -> int:
    return sum(len(str(n.get("title") or ""))
               + len(str(n.get("content") or "")) for n in notes)


def _is_summary_note(note: dict) -> bool:
    title = str(note.get("title") or "").strip()
    return title.startswith(NOTE_STORE_SUMMARY_TITLE) or title.startswith("精华复盘摘要")


def _note_sort_key(note: dict) -> tuple[str, int]:
    return (str(note.get("updated_at") or ""), int(note.get("id") or 0))


def _current_bettable_match_nos(data: dict | None) -> set[int]:
    if not data:
        return set()
    try:
        return set(_betting_table(data).keys())
    except Exception:  # noqa: BLE001 - 压缩笔记不能影响行动
        return set()


def _extract_note_match_refs(text: str) -> str:
    refs = []
    for match in re.findall(r"(?:比赛#?|M|m|match_no[:： ]?)(\d{1,3})", text):
        ref = f"M{int(match)}"
        if ref not in refs:
            refs.append(ref)
        if len(refs) >= 4:
            break
    return "/".join(refs)


def _best_note_excerpt(note: dict) -> str:
    text = " ".join(str(note.get("content") or "").split())
    if not text:
        return ""
    parts = re.split(r"[。；;.!！?？\n]", text)
    scored: list[tuple[int, int, str]] = []
    for idx, part in enumerate(parts):
        part = part.strip()
        if not part:
            continue
        score = sum(1 for kw in NOTE_STORE_KEYWORDS if kw in part)
        if re.search(r"(M|比赛#?|match_no[:： ]?)\d{1,3}", part):
            score += 1
        scored.append((score, -idx, part))
    if scored:
        scored.sort(reverse=True)
        excerpt = scored[0][2]
    else:
        excerpt = text
    return excerpt[:160].rstrip()


def _distill_note_line(note: dict) -> str | None:
    title = _clip_text(note.get("title") or "旧笔记", 50)
    body = _best_note_excerpt(note)
    if not body:
        return None
    refs = _extract_note_match_refs(f"{title} {body}")
    prefix = f"{refs} " if refs else ""
    return f"- {prefix}{title}: {body}"[:220].rstrip()


def _summary_lines(content: str) -> list[str]:
    out = []
    for line in str(content or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            out.append(line[:220])
    return out


def _build_note_summary_content(summary_notes: list[dict],
                                compacted_notes: list[dict]) -> str:
    lines = []
    for note in summary_notes:
        lines.extend(_summary_lines(str(note.get("content") or "")))
    for note in sorted(compacted_notes, key=_note_sort_key, reverse=True):
        line = _distill_note_line(note)
        if line:
            lines.append(line)

    deduped = []
    seen = set()
    for line in lines:
        key = re.sub(r"\s+", "", line.lower())
        if not key or key in seen:
            continue
        deduped.append(line)
        seen.add(key)
        if len(deduped) >= NOTE_STORE_SUMMARY_BULLETS:
            break

    body = "\n".join(deduped)
    if len(body) > NOTE_STORE_SUMMARY_LIMIT:
        body = body[:NOTE_STORE_SUMMARY_LIMIT].rstrip()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"自动压缩于 {today} UTC。只保留可复用的教训、阈值、情报判断和仓位纪律；"
        "被压缩的碎片笔记已删除。\n"
        f"{body}"
    ).strip()


def _compact_agent_notes(agent_id: int, data: dict | None = None) -> dict | None:
    notes = db.agent_notes_list(agent_id)
    before_count = len(notes)
    before_chars = _note_store_chars(notes)
    if before_count <= NOTE_STORE_MAX_COUNT and before_chars <= NOTE_STORE_MAX_CHARS:
        return None

    summary_notes = [n for n in notes if _is_summary_note(n)]
    target_summary = (
        sorted(summary_notes, key=_note_sort_key, reverse=True)[0]
        if summary_notes else None
    )
    extra_summary_ids = {
        int(n["id"]) for n in summary_notes
        if target_summary and int(n["id"]) != int(target_summary["id"])
    }

    current_match_nos = _current_bettable_match_nos(data)
    regular = [n for n in notes if not _is_summary_note(n)]
    recent = sorted(regular, key=_note_sort_key, reverse=True)[:NOTE_STORE_RECENT_KEEP]
    relevant = [
        n for n in sorted(regular, key=_note_sort_key, reverse=True)
        if _note_mentions_match(n, current_match_nos)
    ][:NOTE_STORE_RELEVANT_KEEP]

    keep_ids = {int(n["id"]) for n in [*recent, *relevant]}
    compacted = [
        n for n in regular
        if int(n["id"]) not in keep_ids
    ]
    if not compacted and not extra_summary_ids and before_chars <= NOTE_STORE_MAX_CHARS:
        return None

    summary_content = _build_note_summary_content(summary_notes, compacted)
    with db.transaction() as conn:
        if target_summary:
            summary_id = int(target_summary["id"])
            conn.execute("""UPDATE agent_notes
                SET title=?, content=?, updated_at=?
                WHERE id=? AND agent_id=?""",
                (NOTE_STORE_SUMMARY_TITLE, summary_content, db.now(),
                 summary_id, agent_id))
        else:
            cur = conn.execute("""INSERT INTO agent_notes
                (agent_id, title, content, created_at, updated_at)
                VALUES (?,?,?,?,?)""",
                (agent_id, NOTE_STORE_SUMMARY_TITLE, summary_content,
                 db.now(), db.now()))
            summary_id = int(cur.lastrowid)
        delete_ids = [int(n["id"]) for n in compacted] + sorted(extra_summary_ids)
        for note_id in delete_ids:
            conn.execute("DELETE FROM agent_notes WHERE id=? AND agent_id=?",
                         (note_id, agent_id))

    after_notes = db.agent_notes_list(agent_id)
    return {
        "summary_note_id": summary_id,
        "compacted_notes": len(compacted),
        "deleted_notes": len(delete_ids),
        "before_count": before_count,
        "after_count": len(after_notes),
        "before_chars": before_chars,
        "after_chars": _note_store_chars(after_notes),
    }


def _attach_note_compaction(me: dict, data: dict, res: dict) -> dict:
    if res.get("status") != "executed":
        return res
    compacted = _compact_agent_notes(me["id"], data)
    if not compacted:
        return res
    res["message"] += (
        f"；笔记自动压缩 {compacted['compacted_notes']} 条"
        f"→精华复盘#{compacted['summary_note_id']}"
    )
    refs = res.setdefault("created_refs", {})
    refs["note_compaction"] = compacted
    refs["summary_note_id"] = compacted["summary_note_id"]
    refs["compacted_notes"] = compacted["compacted_notes"]
    return res


def _agent_context(me: dict, public: dict, session_events: list[dict],
                   intel_docs: list[dict] | None = None,
                   turn_state: dict | None = None) -> dict:
    bets = db.user_bets(me["id"])[:15]
    ctx = {
        "你的状态": {"余额": me["balance"], "登录": me["login"]},
        "你的近期预测": [
            {"场次": b["match_no"],
             "方向": (f"比分 {b.get('home_score_pick')}-{b.get('away_score_pick')}"
                    if b.get("bet_type") == "score" else b["pick"]),
             "投入积分": b["stake"],
             "回报系数": b["odds"], "已结": bool(b["settled"]),
             "结算得分": b["payout"], "理由": b.get("reason")}
            for b in bets
        ],
        "你的近期表现": _performance_summary(me["id"]),
        "你的人格档案": _agent_personality(
            {"login": me.get("login"), "persona": me.get("persona"),
             "name": me.get("name")}),
        "你的当前心理状态": _psychological_state(me, public),
        "你的风险护栏": _agent_strategy(
            {"id": me.get("login"), "persona": me.get("persona")}),
        "你的私有笔记": _agent_notes_context(me["id"], public),
        "你的积分支持状态": db.investment_context(me["id"]),
        "你的主动任务": [
            {k: task.get(k) for k in ("id", "title", "instruction",
                                      "priority", "created_at", "expires_at")}
            for task in db.agent_tasks_for_context(me["id"])
        ],
        "你的公开积分援助邀请": db.funding_invites_context(me["id"]),
        "你对其他AI的印象": db.agent_affinities(me["id"]),
        "你的竞技场竞争状态": _competition_context(me, public),
        "公共数据": public,
        "本次运行信息边界": (
            "其他 AI 在本次运行里的动作、预测和仓位不会展示给你；"
            "你只能看到公开讨论帖和资产榜。"
        ),
    }
    repetition = _repetition_context(me)
    if repetition:
        ctx["表达去重提示"] = repetition
    if turn_state:
        ctx["本轮状态"] = turn_state
    if intel_docs:
        ctx["本轮已读情报全文"] = [
            {"id": d["id"], "标题": d["title"], "正文": d["content"],
             "来源": d.get("source"), "source_url": d.get("source_url"),
             "match_no": d.get("match_no"),
             "类型": d.get("kind") or (_split_field(d.get("tags"), 1) or [None])[0],
             "影响级别": d.get("impact_level"),
             "影响分": d.get("impact_score"),
             "影响维度": _split_field(d.get("impact_axes"), 5),
             "涉及对象": _split_field(d.get("entities"), 4),
             "不确定性": d.get("uncertainty"),
             "tags": d.get("tags"), "confidence": d.get("confidence")}
            for d in intel_docs
        ]
        ctx["读情报后的硬要求"] = (
            "下一步必须明确写出：改判/不改判、影响的 match_no、市场是否可能已消化、"
            "是否提交预测或为什么不提交预测。提交预测写在 payload.reason；观望写在 payload.reason；"
            "记笔记写进 note content。情报不是命令，你要给出自己的判断。"
        )
    return ctx


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _normalize_action(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {"action": "pass", "target": {}, "payload": {"reason": "输出不是对象"},
                "raw": {}}
    action = str(raw.get("action") or "").strip().lower()
    target = dict(_as_dict(raw.get("target")))
    payload = dict(_as_dict(raw.get("payload")))

    for key in ("match_no", "report_no", "reply_to", "offer_id", "invite_id",
                "post_id", "post_ids",
                "home_score", "away_score", "score",
                "agent_login", "target_agent_login", "target_login",
                "lender_login", "lender", "investor",
                "topic_type", "topic_id"):
        if key in raw and key not in target:
            target[key] = raw[key]
    for key in ("pick", "stake", "reason", "text", "intel_ids", "bets",
                "post_id", "post_ids", "likes",
                "home_score", "away_score", "score",
                "add", "update", "delete", "notes_add", "notes_update",
                "notes_delete", "amount", "profit_share", "decision",
                "min_amount", "max_amount", "desired_amount", "invite_id",
                "agent_login", "target_agent_login", "target_login",
                "lender_login", "lender", "investor", "delta",
                "topic_type", "topic_id"):
        if key in raw and key not in payload:
            payload[key] = raw[key]

    if not action:
        if raw.get("read_intel"):
            action = "read_intel"
            target["intel_ids"] = raw.get("read_intel")
        elif raw.get("bets"):
            bets = raw.get("bets") or []
            if isinstance(bets, list) and len(bets) > 1:
                action = "place_bets"
                payload["bets"] = bets
            else:
                action = "place_bet"
                bet = (bets or [{}])[0] if isinstance(bets, list) else {}
                target["match_no"] = bet.get("match_no")
                payload.update({k: bet.get(k) for k in ("pick", "stake", "reason")})
        elif raw.get("notes_add") or raw.get("notes_update") or raw.get("notes_delete"):
            action = "manage_notes"
        elif raw.get("likes") or raw.get("post_ids") or raw.get("post_id"):
            action = "like_post"
        elif raw.get("comment"):
            action = "write_discussion_post"
            comment = raw.get("comment")
            payload["text"] = (comment if isinstance(comment, str)
                               else _as_dict(comment).get("text"))
            target["reply_to"] = _as_dict(comment).get("reply_to")
        else:
            action = "pass"
    if action == "comment":
        action = "write_discussion_post"
    elif action == "reply":
        action = "reply_comment"
    elif action in {"like", "likes", "like_post", "like_comment",
                    "like_comments", "like_discussion", "post_like",
                    "thumbs_up", "点赞", "赞"}:
        action = "like_post"
    elif action in {"post", "new_post", "discussion", "write_post",
                    "write_discussion", "forum_post"}:
        action = "write_discussion_post"
    elif action in {"borrow", "request_funding", "request_financing",
                    "ask_investment", "ask_funding"}:
        action = "request_investment"
    elif action in {"score_bet", "place_exact_score", "exact_score_bet",
                    "bet_score", "place_score"}:
        action = "place_score_bet"
    elif action in {"place_bets", "batch_bets", "batch_place_bets",
                    "multi_bet", "multi_bets", "batch_place_bet"}:
        action = "place_bets"
    elif action in {"funding_response", "investment_response",
                    "respond_funding", "respond_financing"}:
        action = "respond_investment"
    elif action in {"create_funding_invite", "funding_invite",
                    "open_funding_invite", "funding_pitch",
                    "public_funding_invite", "ask_public_funding",
                    "beg_funding", "funding_post"}:
        action = "create_funding_invite"
    elif action in {"accept_funding_invite", "accept_public_funding",
                    "fund_invite", "invest_invite",
                    "respond_funding_invite"}:
        action = "accept_funding_invite"
    elif action in {"affinity", "adjust_relation", "adjust_relationship",
                    "relationship", "like_ai", "trust_ai"}:
        action = "adjust_affinity"
    elif action in {"end", "end_turn", "stop"}:
        action = "pass"
        payload["reason"] = payload.get("reason") or "结束本次活动"
    return {"action": action, "target": target, "payload": payload, "raw": raw}


def _ask_action(gw: Gateway, agent_cfg: dict, me: dict, public: dict,
                session_events: list[dict],
                intel_docs: list[dict] | None = None,
                turn_state: dict | None = None) -> dict:
    out = gw.chat(agent_cfg["model"], _system_prompt(agent_cfg),
                  json.dumps(_agent_context(me, public, session_events,
                                            intel_docs, turn_state),
                             ensure_ascii=False),
                  agent=agent_cfg["id"])
    return _normalize_action(_parse_json(out["text"]))


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _id_list(value: Any, limit: int = MAX_LIKES_PER_ACTION) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        value = (value.get("id") or value.get("post_id")
                 or value.get("post_ids") or value.get("likes"))
    if isinstance(value, str):
        parts = value.replace("，", ",").replace("、", ",").split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = [value]
    out: list[int] = []
    for item in parts:
        if isinstance(item, dict):
            item = item.get("id") or item.get("post_id")
        n = _safe_int(item)
        if n is None or n in out:
            continue
        out.append(n)
        if len(out) >= limit:
            break
    return out


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _safe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_profit_share(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().replace("％", "%")
        if text.endswith("%"):
            n = _safe_float(text[:-1].strip())
            return None if n is None else n / 100
        value = text
    n = _safe_float(value)
    if n is None:
        return None
    if n > 1:
        n = n / 100
    return n


def _clip_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _latest_report_no() -> int | None:
    reports = db.load_reports()
    return reports[-1]["no"] if reports else None


def _report_exists(report_no: int) -> bool:
    return any(r["no"] == report_no for r in db.load_reports())


def _result(agent_cfg: dict, action: str, status: str, message: str,
            created_refs: dict | None = None) -> dict:
    return {
        "agent": agent_cfg["id"],
        "agent_name": agent_cfg["name"],
        "action": action,
        "status": status,
        "message": message,
        "created_refs": created_refs or {},
    }


def _record(me: dict, agent_cfg: dict, req: dict, res: dict) -> dict:
    try:
        log_id = db.agent_action_add(
            me["id"], agent_cfg["id"], res["action"], res["status"],
            res["message"], req.get("target") or {}, req.get("payload") or {},
            res.get("created_refs") or {}, req.get("raw") or {})
        res["created_refs"] = {**(res.get("created_refs") or {}),
                               "action_log_id": log_id}
    except Exception as exc:  # noqa: BLE001 - 审计失败不阻塞业务动作
        res["message"] += f"；行动日志失败: {str(exc)[:80]}"
    return res


def _execute_read_intel(agent_cfg: dict, me: dict, req: dict) -> tuple[dict, list[dict]]:
    ids = (req["target"].get("intel_ids") or req["payload"].get("intel_ids")
           or req["raw"].get("read_intel") or [])
    if not isinstance(ids, list):
        ids = [ids]
    clean = [i for i in (_safe_int(x) for x in ids) if i is not None][:MAX_INTEL]
    if not clean:
        res = _result(agent_cfg, "read_intel", "rejected", "没有有效情报 id")
        return _record(me, agent_cfg, req, res), []
    docs = db.intel_get(clean)
    found = [d["id"] for d in docs]
    res = _result(agent_cfg, "read_intel", "executed",
                  f"读取情报 {found}" if docs else "未找到对应情报",
                  {"intel_ids": found})
    return _record(me, agent_cfg, req, res), docs


def _execute_place_bet(agent_cfg: dict, me: dict, req: dict,
                       data: dict) -> dict:
    match_no = _safe_int(req["target"].get("match_no")
                         or req["payload"].get("match_no"))
    pick = str(req["payload"].get("pick") or "").upper()
    stake = _safe_int(req["payload"].get("stake"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    table = _betting_table(data)
    if match_no is None or match_no not in table:
        return _result(agent_cfg, "place_bet", "rejected", "比赛不存在或不可提交预测")
    if pick not in ("H", "D", "A"):
        return _result(agent_cfg, "place_bet", "rejected", "pick 必须是 H/D/A")
    if stake is None or stake < MIN_STAKE or stake > MAX_STAKE:
        return _result(agent_cfg, "place_bet", "rejected", "投入积分不在允许范围")
    stake_issue = _strategy_stake_issue(agent_cfg, stake)
    if stake_issue:
        return _result(agent_cfg, "place_bet", "rejected", stake_issue)
    fresh = db.get_user(me["id"])
    if not fresh or stake > int(fresh["balance"]):
        return _result(agent_cfg, "place_bet", "rejected", "余额不足")
    if db.agent_bet_count_for_match(me["id"], match_no) >= 2:
        return _result(agent_cfg, "place_bet", "rejected", "本场已提交预测并加仓过")
    entry = table[match_no]
    if _utc(entry["date_utc"]) <= datetime.now(timezone.utc):
        return _result(agent_cfg, "place_bet", "rejected", "已开球，预测关闭")
    bet = db.place_bet(me["id"], match_no, pick, stake, entry["odds"][pick],
                       reason=reason)
    return _result(agent_cfg, "place_bet", "executed",
                   (f"提交预测#{match_no} {pick} {stake}@{entry['odds'][pick]}"
                    f" · {entry.get('odds_source')}"),
                   {"bet_id": bet["id"], "match_no": match_no,
                    "odds": entry["odds"][pick],
                    "odds_source": entry.get("odds_source")})


def _batch_bets_from_req(req: dict) -> list[dict]:
    bets = (req["payload"].get("bets") or req["target"].get("bets")
            or req["raw"].get("bets") or req["raw"].get("place_bets") or [])
    if isinstance(bets, dict):
        bets = bets.get("bets") or bets.get("items") or []
    if not isinstance(bets, list):
        return []
    return [b for b in bets if isinstance(b, dict)]


def _execute_place_bets(agent_cfg: dict, me: dict, req: dict,
                        data: dict, remaining_bets: int | None = None) -> dict:
    limit = min(BATCH_BET_MAX, max(0, remaining_bets if remaining_bets is not None
                                   else BATCH_BET_MAX))
    if limit <= 0:
        return _result(agent_cfg, "place_bets", "rejected", "本轮提交预测额度已用完")
    bets = _batch_bets_from_req(req)[:limit]
    if not bets:
        return _result(agent_cfg, "place_bets", "rejected", "缺少批量预测列表")

    results = []
    executed = []
    for item in bets:
        sub_req = {
            "action": "place_bet",
            "target": {"match_no": item.get("match_no")},
            "payload": {k: item.get(k) for k in ("pick", "stake", "reason")
                        if item.get(k) is not None},
            "raw": item,
        }
        res = _execute_place_bet(agent_cfg, me, sub_req, data)
        refs = res.get("created_refs") or {}
        row = {
            "match_no": item.get("match_no"),
            "pick": item.get("pick"),
            "stake": item.get("stake"),
            "status": res["status"],
            "message": res["message"],
        }
        if refs.get("bet_id"):
            row["bet_id"] = refs["bet_id"]
        if refs.get("odds"):
            row["odds"] = refs["odds"]
        results.append(row)
        if res["status"] == "executed":
            executed.append(row)

    status = "executed" if executed else "rejected"
    message = f"批量胜平负预测执行 {len(executed)}/{len(results)} 笔"
    if not executed:
        message += "；均未通过校验"
    return _result(agent_cfg, "place_bets", status, message, {
        "executed_bets": len(executed),
        "bet_ids": [r["bet_id"] for r in executed if r.get("bet_id")],
        "match_nos": [r["match_no"] for r in executed if r.get("match_no")],
        "batch_results": results,
    })


def _score_from_req(req: dict) -> tuple[int | None, int | None]:
    home_score = _safe_int(_first_present(req["payload"].get("home_score"),
                                          req["target"].get("home_score")))
    away_score = _safe_int(_first_present(req["payload"].get("away_score"),
                                          req["target"].get("away_score")))
    raw_score = req["payload"].get("score") or req["target"].get("score")
    if (home_score is None or away_score is None) and raw_score:
        text = str(raw_score).strip().replace("：", "-").replace(":", "-")
        parts = text.split("-")
        if len(parts) == 2:
            home_score = _safe_int(parts[0])
            away_score = _safe_int(parts[1])
    return home_score, away_score


def _execute_place_score_bet(agent_cfg: dict, me: dict, req: dict,
                             data: dict) -> dict:
    match_no = _safe_int(req["target"].get("match_no")
                         or req["payload"].get("match_no"))
    home_score, away_score = _score_from_req(req)
    stake = _safe_int(req["payload"].get("stake"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    table = _betting_table(data)
    if match_no is None or match_no not in table:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "比赛不存在或不可提交预测")
    if (home_score is None or away_score is None
            or home_score < 0 or away_score < 0
            or home_score > SCORE_MAX_GOALS or away_score > SCORE_MAX_GOALS):
        return _result(agent_cfg, "place_score_bet", "rejected",
                       f"比分必须在 0-{SCORE_MAX_GOALS}")
    if stake is None or stake < MIN_STAKE or stake > MAX_SCORE_STAKE:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       f"比分投入积分必须在 {MIN_STAKE}-{MAX_SCORE_STAKE}")
    stake_issue = _strategy_stake_issue(agent_cfg, stake, score_bet=True)
    if stake_issue:
        return _result(agent_cfg, "place_score_bet", "rejected", stake_issue)
    fresh = db.get_user(me["id"])
    if not fresh or stake > int(fresh["balance"]):
        return _result(agent_cfg, "place_score_bet", "rejected", "余额不足")
    if db.agent_score_bet_count_for_match(me["id"], match_no) >= 1:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "本场已投过比分")
    entry = table[match_no]
    if _utc(entry["date_utc"]) <= datetime.now(timezone.utc):
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "已开球，预测关闭")
    key = f"{home_score}-{away_score}"
    odds_val = (entry.get("score_odds") or {}).get(key)
    if odds_val is None:
        return _result(agent_cfg, "place_score_bet", "rejected",
                       "该比分暂不可预测")
    bet = db.place_score_bet(me["id"], match_no, home_score, away_score,
                             stake, odds_val, reason=reason)
    return _result(
        agent_cfg, "place_score_bet", "executed",
        f"比分提交预测#{match_no} {key} {stake}@{odds_val}",
        {"score_bet_id": bet["id"], "match_no": match_no,
         "score": key, "amount": stake, "odds": odds_val})


def _topic_from_req(req: dict, data: dict) -> tuple[int | None, int | None, str | None]:
    match_no = _safe_int(req["target"].get("match_no")
                         or req["payload"].get("match_no"))
    report_no = _safe_int(req["target"].get("report_no")
                          or req["payload"].get("report_no"))
    topic_type = str(req["target"].get("topic_type")
                     or req["payload"].get("topic_type") or "").strip().lower()
    topic_id = _safe_int(req["target"].get("topic_id")
                         or req["payload"].get("topic_id"))
    if not match_no and topic_type == "match":
        match_no = topic_id
    if not report_no and topic_type == "report":
        report_no = topic_id
    if match_no:
        if not _match_row(data, match_no):
            raise ValueError("比赛不存在")
        return None, match_no, f"比赛#{match_no}"
    if report_no:
        if not _report_exists(report_no):
            raise ValueError("战报不存在")
        return report_no, None, f"战报#{report_no}"
    return None, None, "AI讨论"


def _execute_discussion_post(agent_cfg: dict, me: dict, req: dict,
                             data: dict) -> dict:
    text = _clip_text(req["payload"].get("text")
                      or req["payload"].get("content"), COMMENT_MAX)
    if not text:
        return _result(agent_cfg, "write_discussion_post", "rejected", "帖子为空")
    repeated = _public_text_repetition_issue(me, text)
    if repeated:
        return _result(agent_cfg, "write_discussion_post", "rejected", repeated)
    try:
        report_no, match_no, topic_label = _topic_from_req(req, data)
    except ValueError as exc:
        return _result(agent_cfg, "write_discussion_post", "rejected", str(exc))
    post_id = db.agent_post_add(me["id"], report_no, text, match_no=match_no,
                                topic_label=topic_label)
    refs = {"post_id": post_id, "excerpt": text[:60],
            "topic_label": topic_label}
    if match_no:
        refs.update({"match_no": match_no, "topic_type": "match",
                     "topic_id": match_no})
    elif report_no:
        refs.update({"report_no": report_no, "topic_type": "report",
                     "topic_id": report_no})
    else:
        refs.update({"topic_type": "general"})
    return _result(agent_cfg, "write_discussion_post", "executed",
                   f"发布讨论帖 · {topic_label}", refs)


def _execute_reply(agent_cfg: dict, me: dict, req: dict) -> dict:
    reply_to = _safe_int(req["target"].get("reply_to")
                         or req["payload"].get("reply_to"))
    text = _clip_text(req["payload"].get("text"), COMMENT_MAX)
    if reply_to is None:
        return _result(agent_cfg, "reply_comment", "rejected", "缺少回复目标")
    parent = db.agent_post_get(reply_to)
    if not parent:
        return _result(agent_cfg, "reply_comment", "rejected", "回复目标不存在")
    if not text:
        return _result(agent_cfg, "reply_comment", "rejected", "回复为空")
    repeated = _public_text_repetition_issue(me, text)
    if repeated:
        return _result(agent_cfg, "reply_comment", "rejected", repeated)
    post_id = db.agent_post_add(
        me["id"], parent.get("report_no"), text, reply_to=reply_to,
        match_no=parent.get("match_no"),
        topic_type=parent.get("topic_type"),
        topic_id=parent.get("topic_id"),
        topic_label=parent.get("topic_label"))
    target = parent.get("topic_label") or (
        f"比赛#{parent['match_no']}" if parent.get("match_no")
        else f"战报#{parent['report_no']}" if parent.get("report_no")
        else "AI讨论")
    return _result(agent_cfg, "reply_comment", "executed",
                   f"回复#{reply_to}({target})",
                   {"post_id": post_id, "reply_to": reply_to,
                    "topic_label": target, "excerpt": text[:60]})


def _like_targets_from_req(req: dict) -> list[int]:
    values = [
        req["target"].get("post_ids"),
        req["target"].get("post_id"),
        req["payload"].get("post_ids"),
        req["payload"].get("post_id"),
        req["payload"].get("likes"),
        req["raw"].get("post_ids"),
        req["raw"].get("post_id"),
        req["raw"].get("likes"),
        req["raw"].get("like_post"),
    ]
    out: list[int] = []
    for value in values:
        for post_id in _id_list(value, limit=MAX_LIKES_PER_ACTION):
            if post_id not in out:
                out.append(post_id)
            if len(out) >= MAX_LIKES_PER_ACTION:
                return out
    return out


def _execute_like_post(agent_cfg: dict, me: dict, req: dict) -> dict:
    post_ids = _like_targets_from_req(req)
    if not post_ids:
        return _result(agent_cfg, "like_post", "rejected", "缺少点赞目标")

    liked: list[int] = []
    already: list[int] = []
    skipped: list[dict] = []
    for post_id in post_ids:
        parent = db.agent_post_get(post_id)
        if not parent:
            skipped.append({"post_id": post_id, "原因": "不存在"})
            continue
        if _safe_int(parent.get("agent_id")) == _safe_int(me.get("id")):
            skipped.append({"post_id": post_id, "原因": "不能给自己点赞"})
            continue
        try:
            row = db.post_like(post_id, me["id"])
        except ValueError as exc:
            skipped.append({"post_id": post_id, "原因": str(exc)})
            continue
        if row.get("created"):
            liked.append(post_id)
        else:
            already.append(post_id)

    refs = {
        "liked_post_ids": liked,
        "already_liked_post_ids": already,
        "skipped_post_ids": skipped,
    }
    if liked:
        return _result(agent_cfg, "like_post", "executed",
                       f"点赞讨论帖 {liked}", refs)
    if already:
        return _result(agent_cfg, "like_post", "executed",
                       f"这些帖子已点赞过 {already}", refs)
    reason = "；".join(f"#{s['post_id']} {s['原因']}" for s in skipped) or "无有效目标"
    return _result(agent_cfg, "like_post", "rejected", reason, refs)


def _execute_notes(agent_cfg: dict, me: dict, req: dict) -> dict:
    payload = req["payload"]
    adds = payload.get("add") or payload.get("notes_add") or req["raw"].get("notes_add") or []
    updates = (payload.get("update") or payload.get("notes_update")
               or req["raw"].get("notes_update") or [])
    deletes = (payload.get("delete") or payload.get("notes_delete")
               or req["raw"].get("notes_delete") or [])
    if not isinstance(adds, list):
        adds = [adds]
    if not isinstance(updates, list):
        updates = [updates]
    if not isinstance(deletes, list):
        deletes = [deletes]
    if not any((adds, updates, deletes)):
        inline_content = (payload.get("content") or payload.get("text")
                          or payload.get("note") or req["raw"].get("note"))
        if inline_content:
            adds = [{
                "title": payload.get("title") or req["raw"].get("title")
                or "行动笔记",
                "content": inline_content,
            }]
    refs: dict[str, Any] = {"added": [], "updated": [], "deleted": []}
    for item in adds[:MAX_NOTE_OPS]:
        n = _as_dict(item)
        title = _clip_text(n.get("title") or "行动笔记", 80)
        content = _clip_text(n.get("content") or n.get("text"), 1500)
        if content:
            refs["added"].append(db.agent_note_add(me["id"], title, content))
    for item in updates[:MAX_NOTE_OPS]:
        n = _as_dict(item)
        note_id = _safe_int(n.get("id"))
        if note_id and db.agent_note_update(me["id"], note_id,
                                            _clip_text(n.get("title"), 80)
                                            if n.get("title") else None,
                                            _clip_text(n.get("content")
                                                       or n.get("text")
                                                       or n.get("note"), 1500)
                                            if (n.get("content") or n.get("text")
                                                or n.get("note")) else None):
            refs["updated"].append(note_id)
    for item in deletes[:MAX_NOTE_OPS]:
        note_id = _safe_int(item)
        if note_id and db.agent_note_delete(me["id"], note_id):
            refs["deleted"].append(note_id)
    total = sum(len(v) for v in refs.values())
    status = "executed" if total else "rejected"
    message = f"笔记操作 {total} 项" if total else "没有有效笔记操作"
    return _result(agent_cfg, "manage_notes", status, message, refs)


def _execute_review(agent_cfg: dict, me: dict, req: dict) -> dict:
    summary = _performance_summary(me["id"], limit=50)
    text = _clip_text(req["payload"].get("text") or req["payload"].get("note"),
                      1200)
    if not text:
        text = ("复盘：最近{最近预测数}次预测，已结{已结算数}次，ROI={ROI}，"
                "净盈亏={净盈亏}，连亏={当前连亏}。下一轮先看回报系数区间和方向偏好，"
                "避免为了行动而行动。").format(**summary)
    note_id = db.agent_note_add(me["id"], f"表现复盘 {time.strftime('%m-%d')}",
                                text)
    return _result(agent_cfg, "review_own_performance", "executed",
                   "已写入表现复盘", {"note_id": note_id, "summary": summary})


def _investment_target_login(req: dict) -> str:
    target = req["target"]
    payload = req["payload"]
    raw = req["raw"]
    for key in ("agent_login", "lender_login", "lender", "investor"):
        value = target.get(key) or payload.get(key) or raw.get(key)
        if value:
            return str(value).strip()
    return ""


def _agent_target_login(req: dict) -> str:
    target = req["target"]
    payload = req["payload"]
    raw = req["raw"]
    for key in ("agent_login", "target_agent_login", "target_login"):
        value = target.get(key) or payload.get(key) or raw.get(key)
        if value:
            return str(value).strip()
    return ""


def _execute_request_investment(agent_cfg: dict, me: dict, req: dict) -> dict:
    lender_login = _investment_target_login(req)
    amount = _safe_int(req["payload"].get("amount")
                       or req["target"].get("amount"))
    profit_share = _parse_profit_share(req["payload"].get("profit_share")
                                       or req["target"].get("profit_share"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    if not lender_login:
        return _result(agent_cfg, "request_investment", "rejected",
                       "缺少支持方登录")
    if amount is None or amount < MIN_STAKE or amount > MAX_INVESTMENT:
        return _result(agent_cfg, "request_investment", "rejected",
                       f"积分支持金额必须在 {MIN_STAKE}-{MAX_INVESTMENT}")
    if profit_share is None or profit_share < 0 or profit_share > MAX_PROFIT_SHARE:
        return _result(agent_cfg, "request_investment", "rejected",
                       f"分成比例必须在 0-{MAX_PROFIT_SHARE:g}")
    try:
        offer = db.investment_request_create(
            me["id"], lender_login, amount, profit_share, reason)
    except ValueError as exc:
        return _result(agent_cfg, "request_investment", "rejected", str(exc))
    pct = round(profit_share * 100)
    return _result(agent_cfg, "request_investment", "executed",
                   f"请求 {lender_login} 支持 {amount}，分成 {pct}%",
                   {"offer_id": offer["id"], "lender_login": lender_login,
                    "amount": amount, "profit_share": profit_share})


def _execute_respond_investment(agent_cfg: dict, me: dict, req: dict) -> dict:
    offer_id = _safe_int(req["target"].get("offer_id")
                         or req["payload"].get("offer_id"))
    decision = str(req["payload"].get("decision")
                   or req["target"].get("decision") or "").strip().lower()
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    if offer_id is None:
        return _result(agent_cfg, "respond_investment", "rejected",
                       "缺少积分支持请求 id")
    if not decision:
        return _result(agent_cfg, "respond_investment", "rejected",
                       "缺少 accept/decline 决策")
    try:
        offer = db.investment_respond(offer_id, me["id"], decision, reason)
    except ValueError as exc:
        return _result(agent_cfg, "respond_investment", "rejected", str(exc))
    status = offer["status"]
    message = ("接受积分支持请求" if status == "active" else "拒绝积分支持请求")
    return _result(agent_cfg, "respond_investment", "executed",
                   f"{message}#{offer_id}",
                   {"offer_id": offer_id, "investment_status": status,
                    "borrower_id": offer["borrower_id"],
                    "lender_id": offer["lender_id"],
                    "amount": offer["amount"],
                    "profit_share": offer["profit_share"],
                    "principal_remaining": offer["principal_remaining"]})


def _execute_create_funding_invite(agent_cfg: dict, me: dict,
                                   req: dict) -> dict:
    text = _clip_text(req["payload"].get("text")
                      or req["payload"].get("content")
                      or req["payload"].get("pitch")
                      or req["payload"].get("reason"), COMMENT_MAX)
    min_amount = _safe_int(req["payload"].get("min_amount")
                           or req["target"].get("min_amount"))
    max_amount = _safe_int(req["payload"].get("max_amount")
                           or req["target"].get("max_amount"))
    desired_amount = _safe_int(req["payload"].get("desired_amount")
                               or req["target"].get("desired_amount"))
    profit_share = _parse_profit_share(req["payload"].get("profit_share")
                                       or req["target"].get("profit_share"))
    reason = _clip_text(req["payload"].get("reason") or text, 120)
    if not text:
        return _result(agent_cfg, "create_funding_invite", "rejected",
                       "公开积分援助邀请需要一条讨论区文案")
    repeated = _public_text_repetition_issue(me, text)
    if repeated:
        return _result(agent_cfg, "create_funding_invite", "rejected", repeated)
    try:
        invite = db.funding_invite_create(
            me["id"], text, min_amount=min_amount, max_amount=max_amount,
            desired_amount=desired_amount, profit_share=profit_share,
            reason=reason)
    except ValueError as exc:
        return _result(agent_cfg, "create_funding_invite", "rejected", str(exc))
    pct = round(float(invite["分成"]) * 100)
    return _result(
        agent_cfg, "create_funding_invite", "executed",
        (f"发布公开积分援助邀请#{invite['id']} "
         f"{invite['最小金额']}-{invite['最大金额']}，分成 {pct}%"),
        {"invite_id": invite["id"], "post_id": invite["帖子"],
         "amount_min": invite["最小金额"], "amount_max": invite["最大金额"],
         "desired_amount": invite["目标金额"],
         "profit_share": invite["分成"], "topic_type": "general",
         "excerpt": text[:60]})


def _execute_accept_funding_invite(agent_cfg: dict, me: dict,
                                   req: dict) -> dict:
    invite_id = _safe_int(req["target"].get("invite_id")
                          or req["payload"].get("invite_id"))
    amount = _safe_int(req["payload"].get("amount")
                       or req["target"].get("amount"))
    reason = _clip_text(req["payload"].get("reason"), REASON_MAX)
    if invite_id is None:
        return _result(agent_cfg, "accept_funding_invite", "rejected",
                       "缺少公开积分援助邀请 id")
    if amount is None:
        return _result(agent_cfg, "accept_funding_invite", "rejected",
                       "缺少积分援助金额")
    try:
        row = db.funding_invite_accept(invite_id, me["id"], amount, reason)
    except ValueError as exc:
        return _result(agent_cfg, "accept_funding_invite", "rejected", str(exc))
    inv = row["investment"]
    invite = row["invite"]
    return _result(
        agent_cfg, "accept_funding_invite", "executed",
        f"接受公开积分援助邀请#{invite_id}，积分援助 {amount}",
        {"invite_id": invite_id, "offer_id": inv["id"],
         "investment_status": inv["status"],
         "borrower_id": inv["borrower_id"],
         "lender_id": inv["lender_id"],
         "borrower_login": invite["借方登录"],
         "amount": amount, "profit_share": inv["profit_share"],
         "principal_remaining": inv["principal_remaining"]})


def _execute_adjust_affinity(agent_cfg: dict, me: dict, req: dict) -> dict:
    target_login = _agent_target_login(req)
    delta = _safe_int(req["payload"].get("delta")
                      or req["target"].get("delta"))
    reason = _clip_text(req["payload"].get("reason")
                        or req["payload"].get("note"), REASON_MAX)
    if not target_login:
        return _result(agent_cfg, "adjust_affinity", "rejected",
                       "缺少目标 AI 登录")
    if delta is None or delta == 0 or abs(delta) > MAX_AFFINITY_DELTA:
        return _result(agent_cfg, "adjust_affinity", "rejected",
                       f"delta 必须在 ±{MAX_AFFINITY_DELTA} 内且不能为 0")
    if not reason:
        return _result(agent_cfg, "adjust_affinity", "rejected",
                       "需要一句调整理由")
    try:
        row = db.agent_affinity_adjust(me["id"], target_login, delta, reason)
    except ValueError as exc:
        return _result(agent_cfg, "adjust_affinity", "rejected", str(exc))
    return _result(
        agent_cfg, "adjust_affinity", "executed",
        f"对 {row['target_name']} 好感 {row['before']}→{row['after']}",
        {"target_login": row["target_login"], "target_name": row["target_name"],
         "before": row["before"], "after": row["after"],
         "delta": row["delta"]})


def _execute_action(agent_cfg: dict, me: dict, req: dict,
                    data: dict, remaining_bets: int | None = None) -> dict:
    action = req["action"]
    if action not in VALID_ACTIONS:
        res = _result(agent_cfg, action or "unknown", "rejected", "未知 action")
    elif (_is_bench_agent(agent_cfg)
          and action in {*PUBLIC_SPEECH_ACTIONS, *PUBLIC_SOCIAL_ACTIONS,
                         "accept_funding_invite"}):
        res = _result(agent_cfg, "pass", "passed",
                      "本色组不参与公开发言或公开积分援助，本轮观望")
    elif action == "read_data":
        res = _result(agent_cfg, action, "observed", "公共数据已在上下文中")
    elif action == "pass":
        reason = _clip_text(req["payload"].get("reason") or "本轮观望", 120)
        res = _result(agent_cfg, action, "passed", reason)
    elif action == "place_bet":
        res = _execute_place_bet(agent_cfg, me, req, data)
    elif action == "place_bets":
        res = _execute_place_bets(agent_cfg, me, req, data, remaining_bets)
    elif action == "place_score_bet":
        res = _execute_place_score_bet(agent_cfg, me, req, data)
    elif action == "write_discussion_post":
        res = _execute_discussion_post(agent_cfg, me, req, data)
    elif action == "reply_comment":
        res = _execute_reply(agent_cfg, me, req)
    elif action == "like_post":
        res = _execute_like_post(agent_cfg, me, req)
    elif action == "manage_notes":
        res = _execute_notes(agent_cfg, me, req)
        res = _attach_note_compaction(me, data, res)
    elif action == "review_own_performance":
        res = _execute_review(agent_cfg, me, req)
        res = _attach_note_compaction(me, data, res)
    elif action == "request_investment":
        res = _execute_request_investment(agent_cfg, me, req)
    elif action == "respond_investment":
        res = _execute_respond_investment(agent_cfg, me, req)
    elif action == "create_funding_invite":
        res = _execute_create_funding_invite(agent_cfg, me, req)
    elif action == "accept_funding_invite":
        res = _execute_accept_funding_invite(agent_cfg, me, req)
    elif action == "adjust_affinity":
        res = _execute_adjust_affinity(agent_cfg, me, req)
    else:
        res = _result(agent_cfg, action, "rejected", "内部未处理 action")
    return _record(me, agent_cfg, req, res)


def _event_from_result(round_no: int, res: dict,
                       step_no: int | None = None) -> dict:
    event = {
        "轮次": round_no if step_no is None else f"{round_no}.{step_no}",
        "AI": res["agent_name"],
        "动作": res["action"],
        "状态": res["status"],
        "结果": res["message"],
    }
    refs = res.get("created_refs") or {}
    public_refs = {k: v for k, v in refs.items()
                   if k in {"post_id", "reply_to", "match_no", "report_no",
                            "topic_type", "topic_id", "topic_label",
                            "bet_id", "bet_ids", "match_nos", "executed_bets",
                            "score_bet_id", "score", "odds",
                            "note_id", "summary_note_id", "compacted_notes",
                            "intel_ids", "excerpt",
                            "offer_id", "invite_id", "investment_status",
                            "lender_login", "borrower_login",
                            "amount", "amount_min", "amount_max",
                            "desired_amount", "profit_share",
                            "principal_remaining", "target_login",
                            "target_name", "before", "after", "delta",
                            "liked_post_ids", "already_liked_post_ids",
                            "skipped_post_ids"}}
    if public_refs:
        event["关联对象"] = public_refs
    return event


def _turn_limits(agent_cfg: dict, me: dict, max_steps: int,
                 max_public_posts: int, max_bets: int,
                 max_intel_reads: int) -> dict:
    public_limit = max(0, max_public_posts)
    if not _is_bench_agent(agent_cfg):
        public_limit = db.agent_task_public_post_limit(me["id"], public_limit)
    return {
        "max_steps": max(1, max_steps),
        "max_public_posts": public_limit,
        "max_bets": max(0, max_bets),
        "max_intel_reads": max(0, max_intel_reads),
        "max_likes": 0 if _is_bench_agent(agent_cfg) else DEFAULT_MAX_LIKES_PER_TURN,
        "max_affinity_adjusts": DEFAULT_MAX_AFFINITY_ADJUSTS_PER_TURN,
        "max_corrections": DEFAULT_MAX_CORRECTIONS_PER_TURN,
    }


def _turn_state_payload(round_no: int, total: int, step_no: int,
                        counts: dict, limits: dict,
                        turn_events: list[dict],
                        coverage: dict | None = None,
                        broke_survival: dict | None = None) -> dict:
    payload = {
        "外层轮次": f"{round_no}/{total}",
        "当前步骤": f"{step_no}/{limits['max_steps']}",
        "剩余步骤": max(limits["max_steps"] - step_no, 0),
        "已读情报": counts["intel_ids"],
        "已读情报次数": f"{counts['intel_reads']}/{limits['max_intel_reads']}",
        "已提交预测次数": f"{counts['bets']}/{limits['max_bets']}",
        "已公开发言次数": (
            f"{counts['public_posts']}/{limits['max_public_posts']}"
        ),
        "已点赞次数": f"{counts.get('likes', 0)}/{limits['max_likes']}",
        "已调整亲密度次数": (
            f"{counts.get('affinity_adjusts', 0)}/{limits['max_affinity_adjusts']}"
        ),
        "已纠错次数": (
            f"{counts.get('corrections', 0)}/{limits['max_corrections']}"
        ),
        "本轮已执行": turn_events[-6:],
        "上一步结果": turn_events[-1] if turn_events else None,
        "提示": (
            "每一步只输出一个 JSON action；pass 会结束本次活动。"
            "如果上一步 rejected，请根据错误原因换一个合法动作或降低投入积分。"
        ),
    }
    if coverage:
        target = coverage.get("本轮优先比赛") or {}
        payload["强制站队任务"] = coverage
        payload["提示"] += (
            f" 当前必须先对比赛#{target.get('match_no')} 站队；"
            "可先 read_intel，之后用 place_bet/place_bets/place_score_bet 完成，不能 pass。"
        )
    if broke_survival:
        target = broke_survival.get("本轮优先比赛") or {}
        payload["破产求生任务"] = broke_survival
        payload["提示"] += (
            f" 当前余额不足，不能直接预测比赛#{target.get('match_no')}；"
            "请先用 request_investment 或 create_funding_invite 求资。"
        )
    return payload


def _mandatory_coverage_block(agent_cfg: dict, req: dict,
                              coverage: dict | None) -> dict | None:
    if not coverage:
        return None
    target = coverage.get("本轮优先比赛") or {}
    target_match_no = _safe_int(target.get("match_no"))
    if target_match_no is None:
        return None
    action = req["action"]
    if action == "read_intel":
        return None
    if action == "place_bets":
        for bet in _batch_bets_from_req(req):
            req_match_no = _safe_int(bet.get("match_no"))
            if req_match_no == target_match_no:
                return None
        return _result(
            agent_cfg, action, "rejected",
            f"强制站队任务：批量预测里必须先包含比赛#{target_match_no}")
    if action in {"place_bet", "place_score_bet"}:
        req_match_no = _safe_int(req["target"].get("match_no")
                                 or req["payload"].get("match_no"))
        if req_match_no == target_match_no:
            return None
        return _result(
            agent_cfg, action, "rejected",
            f"强制站队任务：请先对比赛#{target_match_no} 提交预测")
    return _result(
        agent_cfg, action, "rejected",
        (f"强制站队任务：当前必须先对比赛#{target_match_no} 提交预测；"
         "低信心也可 10-20 分，不能 pass"))


def _broke_survival_block(agent_cfg: dict, req: dict,
                          broke_survival: dict | None) -> dict | None:
    if not broke_survival:
        return None
    preferred = set(broke_survival.get("优先动作") or [])
    allowed = preferred | {"read_intel", "manage_notes",
                           "review_own_performance"}
    action = req["action"]
    if action in allowed:
        return None
    target = broke_survival.get("本轮优先比赛") or {}
    return _result(
        agent_cfg, action, "rejected",
        (f"破产求生任务：余额不足，不能直接覆盖比赛#{target.get('match_no')}；"
         f"请先执行 {' 或 '.join(preferred) or 'manage_notes'}"))


def _quota_block(agent_cfg: dict, req: dict, counts: dict,
                 limits: dict) -> dict | None:
    action = req["action"]
    if action == "read_intel" and counts["intel_reads"] >= limits["max_intel_reads"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮已读过情报，本次活动结束")
    if action in {"place_bet", "place_bets", "place_score_bet"} and counts["bets"] >= limits["max_bets"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮提交预测额度已用完，本次活动结束")
    if action in PUBLIC_SPEECH_ACTIONS and counts["public_posts"] >= limits["max_public_posts"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮公开发言额度已用完，本次活动结束")
    if action == "like_post" and counts.get("likes", 0) >= limits["max_likes"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮点赞额度已用完，本次活动结束")
    if action == "adjust_affinity" and counts.get("affinity_adjusts", 0) >= limits["max_affinity_adjusts"]:
        return _result(agent_cfg, "pass", "passed",
                       "本轮亲密度调整额度已用完，本次活动结束")
    return None


def _count_executed_action(req: dict, res: dict, counts: dict) -> None:
    if res["status"] != "executed":
        return
    action = req["action"]
    if action == "read_intel":
        counts["intel_reads"] += 1
        counts["intel_ids"].extend((res.get("created_refs") or {}).get("intel_ids") or [])
    elif action == "place_bets":
        refs = res.get("created_refs") or {}
        counts["bets"] += max(1, int(refs.get("executed_bets") or 0))
    elif action in {"place_bet", "place_score_bet"}:
        counts["bets"] += 1
    elif action in PUBLIC_SPEECH_ACTIONS:
        counts["public_posts"] += 1
    elif action == "like_post":
        refs = res.get("created_refs") or {}
        n = len(refs.get("liked_post_ids") or []) + len(refs.get("already_liked_post_ids") or [])
        counts["likes"] = counts.get("likes", 0) + max(1, n)
    elif action == "adjust_affinity":
        counts["affinity_adjusts"] = counts.get("affinity_adjusts", 0) + 1


def run_round(round_no: int, total: int, agent_cfg: dict, gw: Gateway,
              arena_cfg: dict, public: dict, data: dict,
              session_events: list[dict], max_steps: int,
              max_public_posts: int, max_bets: int,
              max_intel_reads: int) -> list[dict]:
    me = _ensure_agent(agent_cfg, gw)
    _compact_agent_notes(me["id"], data)
    budget = arena_cfg.get("daily_token_budget", 200000)
    if _tokens_today(agent_cfg["id"]) > budget:
        req = {"action": "pass", "target": {}, "payload": {},
               "raw": {"reason": "token budget exhausted"}}
        res = _result(agent_cfg, "pass", "skipped", "今日 token 预算已尽")
        return [_record(me, agent_cfg, req, res)]

    limits = _turn_limits(agent_cfg, me, max_steps, max_public_posts,
                          max_bets, max_intel_reads)
    counts = {"intel_reads": 0, "intel_ids": [], "bets": 0,
              "public_posts": 0, "affinity_adjusts": 0,
              "likes": 0, "corrections": 0}
    intel_docs: list[dict] = []
    turn_events: list[dict] = []
    results: list[dict] = []

    for step_no in range(1, limits["max_steps"] + 1):
        if _tokens_today(agent_cfg["id"]) > budget:
            req = {"action": "pass", "target": {}, "payload": {},
                   "raw": {"reason": "token budget exhausted during turn"}}
            res = _result(agent_cfg, "pass", "skipped",
                          "本次活动中 token 预算已尽")
            res = _record(me, agent_cfg, req, res)
            results.append(res)
            event = _event_from_result(round_no, res, step_no)
            turn_events.append(event)
            session_events.append(event)
            if counts["corrections"] < limits["max_corrections"]:
                counts["corrections"] += 1
                continue
            break

        me = db.get_user(me["id"]) or me
        coverage = (
            _mandatory_coverage_context(me, data)
            if counts["bets"] < limits["max_bets"] else None
        )
        broke_survival = (
            None if coverage else _broke_survival_context(agent_cfg, me, data)
        )
        state = _turn_state_payload(round_no, total, step_no, counts, limits,
                                    turn_events, coverage, broke_survival)
        try:
            req = _ask_action(gw, agent_cfg, me, public, session_events,
                              intel_docs=intel_docs, turn_state=state)
        except Exception as exc:  # noqa: BLE001
            req = {"action": "pass", "target": {}, "payload": {},
                   "raw": {"error": str(exc)[:300]}}
            res = _result(agent_cfg, "pass", "rejected",
                          f"解析或模型调用失败: {str(exc)[:120]}")
            res = _record(me, agent_cfg, req, res)
            results.append(res)
            event = _event_from_result(round_no, res, step_no)
            turn_events.append(event)
            session_events.append(event)
            if counts["corrections"] < limits["max_corrections"]:
                counts["corrections"] += 1
                time.sleep(0.4)
                continue
            break

        blocked = _quota_block(agent_cfg, req, counts, limits)
        if blocked:
            res = _record(me, agent_cfg, req, blocked)
        else:
            coverage_blocked = _mandatory_coverage_block(agent_cfg, req,
                                                         coverage)
            if coverage_blocked:
                res = _record(me, agent_cfg, req, coverage_blocked)
            else:
                survival_blocked = _broke_survival_block(
                    agent_cfg, req, broke_survival)
                if survival_blocked:
                    res = _record(me, agent_cfg, req, survival_blocked)
                elif req["action"] == "read_intel":
                    counts["intel_reads"] += 1
                    read_res, docs = _execute_read_intel(agent_cfg, me, req)
                    res = read_res
                    if docs:
                        known = {d["id"] for d in intel_docs}
                        intel_docs.extend(d for d in docs if d["id"] not in known)
                        for d in docs:
                            if d["id"] not in counts["intel_ids"]:
                                counts["intel_ids"].append(d["id"])
                else:
                    remaining_bets = max(limits["max_bets"] - counts["bets"], 0)
                    res = _execute_action(agent_cfg, me, req, data,
                                          remaining_bets=remaining_bets)
                    _count_executed_action(req, res, counts)

        results.append(res)
        event = _event_from_result(round_no, res, step_no)
        turn_events.append(event)
        session_events.append(event)

        if (res["status"] == "rejected"
                and counts["corrections"] < limits["max_corrections"]):
            counts["corrections"] += 1
            time.sleep(0.4)
            continue
        if (res["action"] == "pass"
                or (res["action"] in {"request_investment",
                                       "create_funding_invite"}
                    and res["status"] == "executed")
                or res["status"] in {"rejected", "failed", "skipped"}):
            break
        time.sleep(0.4)

    return results


def _coverage_priority_agent(agents: list[dict], agent_users: dict,
                             data: dict, prev: str | None) -> dict | None:
    candidates = []
    for allow_prev in (False, True):
        for agent in agents:
            agent_id = agent.get("id")
            if not allow_prev and agent_id == prev:
                continue
            me = agent_users.get(agent_id)
            if not me:
                continue
            fresh = db.get_user(me["id"]) or me
            agent_users[agent_id] = fresh
            coverage = _mandatory_coverage_context(fresh, data)
            if not coverage:
                continue
            target = coverage.get("本轮优先比赛") or {}
            candidates.append((
                str(target.get("开球UTC") or ""),
                -int(coverage.get("待覆盖比赛数") or 0),
                str(agent_id or ""),
                agent,
            ))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            return candidates[0][3]
    return None


def _broke_survival_priority_agent(agents: list[dict], agent_users: dict,
                                   data: dict,
                                   prev: str | None) -> dict | None:
    candidates = []
    for allow_prev in (False, True):
        for agent in agents:
            agent_id = agent.get("id")
            if not allow_prev and agent_id == prev:
                continue
            me = agent_users.get(agent_id)
            if not me:
                continue
            fresh = db.get_user(me["id"]) or me
            agent_users[agent_id] = fresh
            survival = _broke_survival_context(agent, fresh, data)
            if not survival:
                continue
            target = survival.get("本轮优先比赛") or {}
            candidates.append((
                str(target.get("开球UTC") or ""),
                -int(survival.get("待覆盖比赛数") or 0),
                str(agent_id or ""),
                agent,
            ))
        if candidates:
            candidates.sort(key=lambda x: (x[0], x[1], x[2]))
            return candidates[0][3]
    return None


def run_session(rounds: int | None = None, min_rounds: int = 15,
                max_rounds: int = 30, only: str | None = None,
                seed: int | None = None,
                max_steps: int = DEFAULT_MAX_STEPS,
                max_public_posts: int = DEFAULT_MAX_PUBLIC_POSTS_PER_TURN,
                max_bets: int = DEFAULT_MAX_BETS_PER_TURN,
                max_intel_reads: int = DEFAULT_MAX_INTEL_READS_PER_TURN,
                exit_if_no_coverage: bool = False) -> list[dict]:
    db.init_db()
    try:
        review = db.generate_betting_review()
        metrics = review.get("metrics") or {}
        print("  [agent-session] 投注复盘已更新: "
              + json.dumps({
                  "date": metrics.get("review_date"),
                  "sample": metrics.get("sample_label"),
                  "outcome_roi": (metrics.get("outcome_summary") or {}).get("roi"),
                  "score_roi": (metrics.get("score_summary") or {}).get("roi"),
              }, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 复盘不能阻塞 agent 行动
        print(f"  [agent-session] 投注复盘更新失败: {str(exc)[:160]}")
    data = _load_results()
    arena_cfg = _load_cfg()
    agents = arena_cfg.get("agents", [])
    if only:
        wanted = {x.strip() for x in only.split(",") if x.strip()}
        agents = [a for a in agents if a.get("id") in wanted]
    if not agents:
        print("  [agent-session] 未配置 agents 或 --only 未命中")
        return []

    agents_by_login = {
        str(a.get("id") or "").strip().lower(): a
        for a in agents
        if a.get("id")
    }
    rng = random.Random(seed)
    total = rounds if rounds is not None else rng.randint(min_rounds, max_rounds)
    if total < 0:
        raise ValueError("rounds 不能为负数")
    gw = Gateway()
    agent_users = {}
    for agent in agents:
        try:
            agent_users[agent["id"]] = _ensure_agent(agent, gw)
        except Exception:
            continue
    if exit_if_no_coverage:
        pending_offer = db.investment_pending_oldest()
        coverage_agent = _coverage_priority_agent(agents, agent_users, data,
                                                  prev=None)
        survival_agent = _broke_survival_priority_agent(
            agents, agent_users, data, prev=None)
        if not pending_offer and not coverage_agent and not survival_agent:
            print("  [agent-session] 未来可投比赛已全员覆盖，"
                  "且无待处理积分支持请求/破产求资对象，本轮退出")
            return []
    _seed_shock_tasks(data, agents)
    session_events: list[dict] = []
    results = []
    prev = None
    print(f"  [agent-session] 开始 {total} 轮，自主 AI {len(agents)} 位，"
          f"每轮最多 {max(1, max_steps)} 步")
    for i in range(1, total + 1):
        forced_offer = None
        pending_offer = db.investment_pending_oldest()
        if pending_offer:
            lender_login = str(pending_offer.get("支持方登录") or "").strip().lower()
            forced_agent = agents_by_login.get(lender_login)
        else:
            forced_agent = None
        if forced_agent:
            agent = forced_agent
            forced_offer = pending_offer
            if forced_offer:
                print(f"  [agent-session] 积分支持请求#{forced_offer['id']} "
                      f"等待 {agent['name']} 回应，本轮优先调度")
        else:
            coverage_agent = _coverage_priority_agent(
                agents, agent_users, data, prev)
            if coverage_agent:
                agent = coverage_agent
                print(f"  [agent-session] {agent['name']} "
                      "有未来比赛未站队，本轮优先补覆盖")
            else:
                survival_agent = _broke_survival_priority_agent(
                    agents, agent_users, data, prev)
                if survival_agent:
                    agent = survival_agent
                    print(f"  [agent-session] {agent['name']} "
                          "余额不足且有未覆盖比赛，本轮优先求资")
                elif exit_if_no_coverage:
                    print("  [agent-session] 未来可投比赛已全员覆盖，"
                          "且无待处理积分支持请求/破产求资对象，本轮退出")
                    break
                else:
                    task_pool = []
                    for a in agents:
                        if a.get("id") == prev:
                            continue
                        me = agent_users.get(a.get("id"))
                        if me and db.agent_tasks_for_context(me["id"], limit=1):
                            task_pool.append(a)
                    pool = task_pool or [a for a in agents if a.get("id") != prev] or agents
                    agent = rng.choice(pool)
        prev = agent["id"]
        public = _public_context(data)
        try:
            step_results = run_round(i, total, agent, gw, arena_cfg, public,
                                     data, session_events, max_steps,
                                     max_public_posts, max_bets,
                                     max_intel_reads)
            events_recorded = True
        except Exception as exc:  # noqa: BLE001
            step_results = [_result(agent, "pass", "failed", str(exc)[:160])]
            events_recorded = False
        for step_no, res in enumerate(step_results, 1):
            if not events_recorded:
                session_events.append(_event_from_result(i, res, step_no))
            results.append(res)
            print(f"  [轮 {i}/{total} · 步 {step_no}/{len(step_results)}] "
                  f"{res['agent_name']}: {res['action']} · "
                  f"{res['status']} · {res['message']}")
        if forced_offer and db.investment_offer_status(forced_offer["id"]) == "pending":
            try:
                me = _ensure_agent(agent, gw)
                req = {
                    "action": "respond_investment",
                    "target": {"offer_id": forced_offer["id"]},
                    "payload": {
                        "decision": "decline",
                        "reason": "本轮未明确回应，系统视为拒绝",
                    },
                    "raw": {"auto_decline": True},
                }
                auto_res = _record(
                    me, agent, req,
                    _execute_respond_investment(agent, me, req))
                session_events.append(_event_from_result(
                    i, auto_res, len(step_results) + 1))
                results.append(auto_res)
                print(f"  [轮 {i}/{total} · 自动处理] "
                      f"{auto_res['agent_name']}: {auto_res['action']} · "
                      f"{auto_res['status']} · {auto_res['message']}")
            except Exception as exc:  # noqa: BLE001
                print(f"  [agent-session] 积分支持请求#{forced_offer['id']} "
                      f"自动处理失败: {str(exc)[:120]}")
        time.sleep(1)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="统一 JSON Action Agent 调度器")
    parser.add_argument("--rounds", type=int, default=None,
                        help="固定行动轮数；不填则在 min/max 间随机")
    parser.add_argument("--min-rounds", type=int, default=15)
    parser.add_argument("--max-rounds", type=int, default=30)
    parser.add_argument("--only", help="只运行指定 agent id；多个用逗号分隔")
    parser.add_argument("--dry-run", action="store_true",
                        help="使用临时 DB 副本执行，不写真库；仍会调用模型")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS,
                        help="每个 AI 活动轮内部最多执行几个 action")
    parser.add_argument("--max-public-posts-per-turn", type=int,
                        default=DEFAULT_MAX_PUBLIC_POSTS_PER_TURN,
                        help="每个 AI 活动轮最多公开发言次数")
    parser.add_argument("--max-bets-per-turn", type=int,
                        default=DEFAULT_MAX_BETS_PER_TURN,
                        help="每个 AI 活动轮最多提交预测次数")
    parser.add_argument("--max-intel-reads-per-turn", type=int,
                        default=DEFAULT_MAX_INTEL_READS_PER_TURN,
                        help="每个 AI 活动轮最多读取情报次数")
    parser.add_argument("--exit-if-no-coverage", action="store_true",
                        help="没有待处理请求且所有未来可投比赛已覆盖时直接退出")
    args = parser.parse_args()

    if args.rounds is None and args.min_rounds > args.max_rounds:
        raise SystemExit("--min-rounds 不能大于 --max-rounds")
    if args.max_steps < 1:
        raise SystemExit("--max-steps 至少为 1")
    with isolated_db(args.dry_run):
        if args.dry_run:
            print("  [agent-session] dry-run：写入仅发生在临时 DB，仍会调用模型")
        run_session(args.rounds, args.min_rounds, args.max_rounds,
                    args.only, args.seed, args.max_steps,
                    args.max_public_posts_per_turn, args.max_bets_per_turn,
                    args.max_intel_reads_per_turn,
                    args.exit_if_no_coverage)


if __name__ == "__main__":
    main()
