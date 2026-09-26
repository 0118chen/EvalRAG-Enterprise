"""Query rewrite adapters with a deterministic offline implementation."""

from dataclasses import dataclass
from typing import Protocol

from app.config import Settings


@dataclass(frozen=True)
class QueryPlan:
    original: str
    queries: list[str]


class QueryRewriter(Protocol):
    def rewrite(self, question: str) -> QueryPlan: ...


class IdentityQueryRewriter:
    def rewrite(self, question: str) -> QueryPlan:
        normalized = " ".join(question.split())
        return QueryPlan(original=question, queries=[normalized])


class RuleBasedQueryRewriter:
    """Expand common enterprise policy terms without requiring an external model."""

    _expansions = (
        ("生效日期", ("实施日期", "开始执行")),
        ("失效日期", ("废止日期", "停止执行")),
        ("申请条件", ("办理条件", "适用条件")),
        ("办理流程", ("申请流程", "操作步骤")),
        ("责任人", ("负责部门", "责任主体")),
        ("审批", ("核准", "审核")),
        ("期限", ("时限", "截止时间")),
        ("不得用于", ("禁止用途", "资金用途", "投资限制")),
        ("哪些事项", ("禁止情形", "用途限制")),
        ("承包期", ("承包期限", "三十年", "延长三十年")),
    )
    max_queries: int = 3

    def rewrite(self, question: str) -> QueryPlan:
        normalized = " ".join(question.split())
        queries = [normalized]
        for trigger, expansions in self._expansions:
            if trigger not in normalized:
                continue
            queries.append(f"{normalized} {' '.join(expansions)}")
            if len(queries) >= self.max_queries:
                break
        unique: list[str] = []
        for query in queries:
            if query and query not in unique:
                unique.append(query)
        return QueryPlan(original=question, queries=unique)


def create_query_rewriter(settings: Settings) -> QueryRewriter:
    if not settings.query_rewrite_enabled:
        return IdentityQueryRewriter()
    return RuleBasedQueryRewriter()
