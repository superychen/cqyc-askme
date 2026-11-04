"""证据门控回归：仅用演示资料和模拟模型，不联网、不读取真实简历或消耗会话额度。

阅读顺序：row 构造证据 → GateTests 初始化请求 → 调用真实 evidence_gate → 校验
路由与传给模型的候选。模型真假判断通过模拟固定，测试的是程序边界而非 LLM 准确率。
执行：python -m unittest discover -s tests -v。
"""
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from askme.rag.chat_graph import evidence_gate
from askme.schemas.chat import EvidenceDecision


def row(identity="rag:demo", score=0.04, source="rag"):
    """构造演示候选；如 score=0.04 返回低分姓名证据，不代表真实模型评分。"""
    return {"id": identity, "score": score, "source_type": source,
            "text": "我叫小林，从事后端开发。", "title": "演示资料", "url": None}


class GateTests(IsolatedAsyncioTestCase):
    """每例独立运行状态；验证低分复核不会跳过来源、ID、事实覆盖和异常检查。"""

    def setUp(self):
        """初始化演示原问题与改写问题；默认非续接，失败应路由 clarify。"""
        self.state = {"question": "你叫什么名字，简单介绍下自己", "intent": "personal",
                      "standalone_query": "求职者姓名和经历", "web_status": "skipped",
                      "ranked": [row()]}
        self.runtime = SimpleNamespace(context=SimpleNamespace(
            settings=SimpleNamespace(rerank_min_score=0.5, answer_context_top_k=6),
            operation={"continuation": False}))

    async def test_low_score_fact_is_reviewed_with_original_question(self):
        """演示低分但有姓名：必须经审核才 answer，原问题和实际分数均保留。"""
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            review.return_value = EvidenceDecision(sufficient=True, allowed_ids=["rag:demo"])
            result = await evidence_gate(self.state, self.runtime)
        self.assertEqual(result["decision"], "answer")
        payload = review.call_args.args[3]
        self.assertEqual(payload["question"], self.state["question"])
        self.assertEqual(payload["standalone_query"], self.state["standalone_query"])
        self.assertEqual(payload["evidence"][0]["score"], 0.04)
        self.assertEqual(self.state["ranked"], [row()])

    async def test_review_rejects_missing_fact_at_any_score(self):
        """演示问工资却只有姓名：低分和高分都不能代替正文中的工资事实。"""
        self.state["question"] = "你的工资是多少？"
        for score in (0.0001, 0.95):
            with self.subTest(score=score):
                self.state["ranked"] = [row(score=score)]
                with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
                    review.return_value = EvidenceDecision(sufficient=False, missing="没有工资记录")
                    result = await evidence_gate(self.state, self.runtime)
                self.assertEqual(result["decision"], "clarify")
                self.assertEqual(result["evidence"], [])

    async def test_personal_filters_web_before_low_score_review(self):
        """演示网页0.99、个人片段0.04：只复核个人片段，网页不能证明履历。"""
        self.state["ranked"] = [row("web:demo", 0.99, "web"), row()]
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            review.return_value = EvidenceDecision(sufficient=True, allowed_ids=["rag:demo"])
            result = await evidence_gate(self.state, self.runtime)
        self.assertEqual(review.call_args.args[3]["evidence"], [row()])
        self.assertEqual(result["decision"], "answer")

    async def test_empty_or_web_only_skips_review(self):
        """空结果和仅有网页均无个人证据；不外呼模型、不制造空候选。"""
        for rows in ([], [row("web:demo", 0.99, "web")]):
            with self.subTest(rows=rows):
                self.state["ranked"] = rows
                with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
                    result = await evidence_gate(self.state, self.runtime)
                review.assert_not_awaited()
                self.assertEqual(result["decision"], "clarify")

    async def test_low_score_review_obeys_context_limit(self):
        """演示20条低分候选、预算6条：只送前6条，避免复核扩大上下文和外呼负担。"""
        self.state["ranked"] = [row(f"rag:demo-{i}") for i in range(20)]
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            review.return_value = EvidenceDecision(sufficient=False)
            await evidence_gate(self.state, self.runtime)
        self.assertEqual(review.call_args.args[3]["evidence"], self.state["ranked"][:6])

    async def test_qualified_rows_keep_existing_selection(self):
        """有过线证据时沿用筛选规则；演示0.8入选、0.04不额外扩入。"""
        self.state["ranked"] = [row("rag:high", 0.8), row()]
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            review.return_value = EvidenceDecision(sufficient=True, allowed_ids=["rag:high"])
            result = await evidence_gate(self.state, self.runtime)
        self.assertEqual(review.call_args.args[3]["evidence"], [row("rag:high", 0.8)])
        self.assertEqual(result["decision"], "answer")

    async def test_invalid_or_empty_ids_do_not_authorize_answer(self):
        """演示 sufficient=true 但 ID 不存在/为空：不能越过可追溯来源校验。"""
        for ids in (["rag:invented"], []):
            with self.subTest(ids=ids):
                with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
                    review.return_value = EvidenceDecision(sufficient=True, allowed_ids=ids)
                    result = await evidence_gate(self.state, self.runtime)
                self.assertEqual(result["decision"], "clarify")
                self.assertEqual(result["evidence"], [])

    async def test_timeout_after_continuation_ends_with_unknown(self):
        """演示续接时审核超时：保守返回 unknown，不能再次澄清或猜测答案。"""
        self.runtime.context.operation["continuation"] = True
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            review.side_effect = TimeoutError()
            result = await evidence_gate(self.state, self.runtime)
        self.assertEqual(result["decision"], "unknown")

    async def test_freshness_failure_skips_even_low_score_review(self):
        """演示最新技术问题联网失败：本地旧片段不能冒充时效证据。"""
        self.state.update(question="目前版本是什么？", intent="technical", web_status="failed")
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            result = await evidence_gate(self.state, self.runtime)
        review.assert_not_awaited()
        self.assertEqual(result["decision"], "clarify")

    async def test_partial_answer_preserves_missing_fields(self):
        """演示姓名有依据、年限缺失：允许部分回答，并把缺失项交给回答节点。"""
        with patch("askme.rag.chat_graph.structured", new_callable=AsyncMock) as review:
            review.return_value = EvidenceDecision(
                sufficient=True, allowed_ids=["rag:demo"], missing="没有准确工作年限")
            result = await evidence_gate(self.state, self.runtime)
        self.assertEqual(result["decision"], "answer")
        self.assertEqual(result["missing"], "没有准确工作年限")
