from __future__ import annotations

import re
import logging
from typing import Any

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models.db import AgentRunAttempt, ChartArtifact, ConversationMessage, GeneratedOutput
from app.services.sandbox_runner import SandboxUnavailableError, get_sandbox_runner
from app.utils.safety import UnsafeCodeError, validate_generated_code

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are StatBot Pro, an autonomous data analyst that writes safe Python code.
Return only executable Python code, without markdown fences or commentary.

Runtime contract:
- A pandas DataFrame named df is already loaded from the uploaded file.
- DATA_PATH is the read-only uploaded file path, but prefer using df directly.
- pd, np, plt, OUTPUT_DIR, emit_insight, emit_table, and emit_chart are available.
- Do not import modules.
- Do not use open, eval, exec, subprocess, os, sys, shutil, pathlib, network calls, shell commands, deletion, or filesystem mutation.
- Write charts only by calling plt.savefig(OUTPUT_DIR / "clear_name.png", bbox_inches="tight").
- After saving a chart, call emit_chart(chart_path, "Short title").
- Use emit_table("Name", dataframe_or_records) for important table outputs.
- Use emit_insight("Plain English answer") for the final user-facing answer.
- Prefer deterministic Pandas and Matplotlib code.
- If a dataset has Variable_name and Value columns, treat Value as the measure. Convert it with pd.to_numeric(..., errors="coerce") after removing commas, and do not use Year as the metric unless the user explicitly asks about years.
- For repeated Year/Variable_name rows, use pivot_table or groupby instead of pivot to avoid duplicate-key failures.
- Never use identifier-like columns such as Order ID, Customer ID, row number, code, zip, or rank as business measures.
- For sales questions, prefer total revenue/revenue/sales/amount over units, unit price, cost, IDs, and years.
- For cost vs price questions, compare total cost with total revenue/total price when available; otherwise use the best cost and price/revenue columns.
- Convert numeric-looking text columns safely before choosing metrics, and derive year/month from date columns for time-series questions.
"""


USER_PROMPT = """Dataset profile:
{profile}

Recent chat history:
{history}

User question:
{question}

Write the safest minimal Python analysis code that answers the question."""


REPAIR_PROMPT = """The previous generated code failed in the sandbox.

Dataset profile:
{profile}

User question:
{question}

Previous code:
{code}

Sandbox error:
{error}

Return corrected Python code only."""


class AgentService:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.settings = get_settings()
        self.runner = get_sandbox_runner()

    def answer_question(self, *, session_id: str, file_id: str, stored_name: str, profile: dict[str, Any], question: str) -> dict:
        history = self._history(session_id, file_id)
        code = self._generate_code(profile=profile, history=history, question=question)
        attempts: list[dict[str, Any]] = []

        for attempt_number in range(1, self.settings.max_agent_retries + 2):
            result = self._execute_attempt(session_id, file_id, question, attempt_number, code, stored_name)
            attempts.append(result)
            if result["success"]:
                return self._build_success(session_id, file_id, result, attempt_number - 1)

            if attempt_number <= self.settings.max_agent_retries:
                code = self._repair_code(profile=profile, question=question, code=code, error=result.get("error") or result.get("stderr", ""))

        last = attempts[-1]
        return {
            "answer": "I could not complete this analysis safely after retrying. The last sandbox error is included for debugging.",
            "tables": [],
            "charts": [],
            "stdout": last.get("stdout", ""),
            "error": last.get("error") or last.get("stderr", ""),
            "retry_count": len(attempts) - 1,
        }

    def persist_outputs(self, *, session_id: str, file_id: str, message_id: str, response: dict[str, Any]) -> dict[str, Any]:
        tables = response.get("tables", [])
        chart_payloads = []

        for table in tables:
            self.db.add(
                GeneratedOutput(
                    session_id=session_id,
                    file_id=file_id,
                    message_id=message_id,
                    output_type="table",
                    payload=table,
                )
            )

        for chart in response.get("charts", []):
            artifact = ChartArtifact(
                session_id=session_id,
                file_id=file_id,
                message_id=message_id,
                title=chart.get("title"),
                relative_path=chart["path"],
            )
            self.db.add(artifact)
            self.db.flush()
            chart_payloads.append({"id": artifact.id, "title": artifact.title, "url": f"/files/{file_id}/chart/{artifact.id}"})
            self.db.add(
                GeneratedOutput(
                    session_id=session_id,
                    file_id=file_id,
                    message_id=message_id,
                    output_type="chart",
                    payload={"id": artifact.id, "title": artifact.title, "path": artifact.relative_path},
                )
            )

        self.db.commit()
        response["charts"] = chart_payloads
        return response

    def _execute_attempt(self, session_id: str, file_id: str, question: str, attempt_number: int, code: str, stored_name: str) -> dict:
        try:
            validate_generated_code(code)
            result = self.runner.run(stored_name, code)
        except UnsafeCodeError as exc:
            result = {"success": False, "stdout": "", "stderr": str(exc), "error": str(exc), "tables": [], "charts": [], "execution_time": 0}
        except SandboxUnavailableError as exc:
            result = {"success": False, "stdout": "", "stderr": str(exc), "error": str(exc), "tables": [], "charts": [], "execution_time": 0}

        self.db.add(
            AgentRunAttempt(
                session_id=session_id,
                file_id=file_id,
                question=question,
                attempt_number=attempt_number,
                generated_code=code,
                success=bool(result.get("success")),
                stdout=result.get("stdout", ""),
                stderr=result.get("stderr", ""),
                error_message=result.get("error"),
                execution_time=result.get("execution_time"),
            )
        )
        self.db.commit()
        return result

    def _build_success(self, session_id: str, file_id: str, result: dict, retry_count: int) -> dict:
        insights = result.get("insights") or []
        answer = "\n\n".join(str(item) for item in insights if item) or result.get("stdout") or "Analysis completed successfully."
        return {
            "answer": answer,
            "tables": result.get("tables", []),
            "charts": result.get("charts", []),
            "stdout": result.get("stdout", ""),
            "retry_count": retry_count,
        }

    def _history(self, session_id: str, file_id: str) -> str:
        rows = self.db.exec(
            select(ConversationMessage)
            .where(ConversationMessage.session_id == session_id, ConversationMessage.file_id == file_id)
            .order_by(ConversationMessage.created_at.desc())
            .limit(8)
        ).all()
        ordered = list(reversed(rows))
        return "\n".join(f"{row.role}: {row.content[:1200]}" for row in ordered)

    def _generate_code(self, *, profile: dict[str, Any], history: str, question: str) -> str:
        if self._has_variable_value_schema(profile):
            return self._fallback_code(question, profile=profile)
        llm_code = self._call_llm(USER_PROMPT.format(profile=profile, history=history or "No previous messages.", question=question))
        return llm_code or self._fallback_code(question, profile=profile)

    def _repair_code(self, *, profile: dict[str, Any], question: str, code: str, error: str) -> str:
        if self._has_variable_value_schema(profile):
            return self._fallback_code(question, profile=profile, conservative=True)
        llm_code = self._call_llm(REPAIR_PROMPT.format(profile=profile, question=question, code=code, error=error))
        return llm_code or self._fallback_code(question, profile=profile, conservative=True)

    def _call_llm(self, prompt: str) -> str | None:
        if not self.settings.openai_api_key:
            return None
        try:
            from langchain_core.output_parsers import StrOutputParser
            from langchain_core.prompts import ChatPromptTemplate
            from langchain_openai import ChatOpenAI

            chat_prompt = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "{input}")])
            model = ChatOpenAI(
                model=self.settings.openai_model,
                temperature=self.settings.agent_temperature,
                api_key=self.settings.openai_api_key,
            )
            chain = chat_prompt | model | StrOutputParser()
            return self._strip_code_fences(chain.invoke({"input": prompt}))
        except Exception:
            logger.exception("LLM code generation failed; falling back to deterministic analysis code.")
            return None

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        cleaned = text.strip()
        match = re.search(r"```(?:python)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
        return match.group(1).strip() if match else cleaned

    @staticmethod
    def _has_variable_value_schema(profile: dict[str, Any]) -> bool:
        columns = {str(col).lower() for col in profile.get("columns", [])}
        return "variable_name" in columns and "value" in columns

    @staticmethod
    def _fallback_code(question: str, profile: dict[str, Any] | None = None, conservative: bool = False) -> str:
        q = question.lower()
        if profile and AgentService._has_variable_value_schema(profile):
            if any(word in q for word in ["plot", "chart", "trend", "graph", "visual"]):
                return f"QUESTION = {question!r}\n{VARIABLE_VALUE_CHART_CODE}"
            if any(word in q for word in ["income", "expenditure", "expense", "financial", "metric", "variable", "value", "top", "highest", "breakdown", "performance"]):
                return f"QUESTION = {question!r}\n{VARIABLE_VALUE_ANALYSIS_CODE}"
        if any(phrase in q for phrase in ["year on year", "year-over-year", "year over year", "yoy"]) or ("year" in q and any(word in q for word in ["sales", "revenue", "trend"])):
            return f"QUESTION = {question!r}\n{FALLBACK_YEAR_CODE}"
        if any(sep in q for sep in [" vs ", " versus ", " compare ", "comparison"]) and any(word in q for word in ["cost", "price", "revenue", "sales", "profit"]):
            return f"QUESTION = {question!r}\n{FALLBACK_COMPARE_CODE}"
        if any(word in q for word in ["plot", "chart", "trend", "graph", "visual"]):
            return f"QUESTION = {question!r}\n{FALLBACK_CHART_CODE}"
        return f"QUESTION = {question!r}\n{FALLBACK_SMART_CODE}"


VARIABLE_VALUE_ANALYSIS_CODE = """
q = QUESTION.lower()
work = df.copy()
value_text = work["Value"].astype(str).str.replace(",", "", regex=False).str.strip()
work["_value_num"] = pd.to_numeric(value_text, errors="coerce")
work["_year_num"] = pd.to_numeric(work["Year"], errors="coerce") if "Year" in work.columns else np.nan
usable = work[work["_value_num"].notna()].copy()
suppressed_count = int(work["_value_num"].isna().sum())

base = usable
if "Units" in base.columns:
    dollars = base[base["Units"].astype(str).str.contains("Dollars", case=False, na=False)].copy()
    if len(dollars):
        base = dollars
if "Industry_name_NZSIOC" in base.columns:
    all_industries = base[base["Industry_name_NZSIOC"].astype(str).str.lower().eq("all industries")].copy()
    if len(all_industries):
        base = all_industries
if "Industry_aggregation_NZSIOC" in base.columns:
    level_one = base[base["Industry_aggregation_NZSIOC"].astype(str).str.lower().eq("level 1")].copy()
    if len(level_one):
        base = level_one

latest_year = int(base["_year_num"].max()) if "_year_num" in base.columns and base["_year_num"].notna().any() else None
latest = base[base["_year_num"].eq(latest_year)].copy() if latest_year is not None else base.copy()
latest["Variable_name"] = latest["Variable_name"].astype(str)
latest["_variable_lower"] = latest["Variable_name"].str.lower()

mentions_income = "income" in q
mentions_expenditure = "expenditure" in q or "expense" in q
mentions_total_pair = mentions_income and mentions_expenditure
mentions_top = "top" in q or "highest" in q or "largest" in q or "best" in q
mentions_performance = "performance" in q
mentions_breakdown = "breakdown" in q or "variable" in q or "metric" in q

unit_label = str(latest["Units"].iloc[0]) if "Units" in latest.columns and len(latest) else "Value"

if mentions_total_pair:
    selected = latest[latest["_variable_lower"].isin(["total income", "total expenditure"])].copy()
    selected = selected[["Variable_name", "_value_num"]].rename(columns={"_value_num": "Value"})
    if len(selected):
        income = selected[selected["Variable_name"].str.lower().eq("total income")]["Value"].sum()
        expenditure = selected[selected["Variable_name"].str.lower().eq("total expenditure")]["Value"].sum()
        comparison = pd.concat(
            [
                selected,
                pd.DataFrame([{"Variable_name": "Income less expenditure", "Value": income - expenditure}]),
            ],
            ignore_index=True,
        )
        year_text = f" in {latest_year}" if latest_year is not None else ""
        emit_insight(f"Total income{year_text} was {income:,.0f} and total expenditure was {expenditure:,.0f}, so income exceeded expenditure by {income - expenditure:,.0f}. Units: {unit_label}.")
        emit_table("Total income vs total expenditure", comparison)
    else:
        emit_insight("I could not find Total income and Total expenditure rows after filtering to the latest all-industries data.")
        emit_table("Available variables", latest[["Variable_name", "_value_num"]].rename(columns={"_value_num": "Value"}).head(30))
elif mentions_income and not mentions_expenditure:
    selected = latest[latest["_variable_lower"].eq("total income")].copy()
    if len(selected):
        value = float(selected["_value_num"].sum())
        year_text = f" in {latest_year}" if latest_year is not None else ""
        emit_insight(f"Total income{year_text} was {value:,.0f}. Units: {unit_label}.")
        emit_table("Total income", selected[["Year", "Variable_name", "Units", "_value_num"]].rename(columns={"_value_num": "Value"}))
    else:
        emit_insight("I could not find a Total income row after filtering to the latest all-industries data.")
elif mentions_top:
    source = latest.copy()
    if mentions_performance and "Variable_category" in source.columns:
        source = source[source["Variable_category"].astype(str).str.lower().eq("financial performance")].copy()
    count = 5 if "5" in q or "five" in q else 10
    columns = ["Variable_name", "_value_num"]
    if "Variable_category" in source.columns and not mentions_performance:
        columns = ["Variable_name", "Variable_category", "_value_num"]
    table = source.sort_values("_value_num", ascending=False)[columns].head(count).rename(columns={"_value_num": "Value"})
    scope = "financial performance " if mentions_performance else ""
    year_text = f" for {latest_year}" if latest_year is not None else ""
    emit_insight(f"Top {count} {scope}metrics by value{year_text}. Units: {unit_label}.")
    emit_table(f"Top {count} metrics by value", table)
elif mentions_breakdown or mentions_performance:
    source = latest.copy()
    if "Variable_category" in source.columns and mentions_performance:
        source = source[source["Variable_category"].astype(str).str.lower().eq("financial performance")].copy()
    table = source.sort_values("_value_num", ascending=False)[["Variable_name", "Variable_category", "Units", "_value_num"]].rename(columns={"_value_num": "Value"})
    year_text = f" for {latest_year}" if latest_year is not None else ""
    emit_insight(f"Financial variable breakdown{year_text}. Units: {unit_label}. Non-numeric/suppressed values ignored: {suppressed_count:,}.")
    emit_table("Financial breakdown by variable name", table, max_rows=50)
else:
    rows, cols = df.shape
    emit_insight(f"The dataset has {rows:,} rows and {cols:,} columns. I detected a survey-style schema where Value is the measure and Variable_name describes each metric. Numeric Value rows: {len(usable):,}; suppressed/non-numeric rows: {suppressed_count:,}.")
    emit_table("Sample rows", df.head(10))
    emit_table("Latest all-industries metrics", latest.sort_values("_value_num", ascending=False)[["Variable_name", "Variable_category", "Units", "_value_num"]].rename(columns={"_value_num": "Value"}), max_rows=25)
""".strip()


VARIABLE_VALUE_CHART_CODE = """
q = QUESTION.lower()
work = df.copy()
value_text = work["Value"].astype(str).str.replace(",", "", regex=False).str.strip()
work["_value_num"] = pd.to_numeric(value_text, errors="coerce")
work["_year_num"] = pd.to_numeric(work["Year"], errors="coerce") if "Year" in work.columns else np.nan
base = work[work["_value_num"].notna()].copy()

if "Units" in base.columns:
    dollars = base[base["Units"].astype(str).str.contains("Dollars", case=False, na=False)].copy()
    if len(dollars):
        base = dollars
if "Industry_name_NZSIOC" in base.columns:
    all_industries = base[base["Industry_name_NZSIOC"].astype(str).str.lower().eq("all industries")].copy()
    if len(all_industries):
        base = all_industries
if "Industry_aggregation_NZSIOC" in base.columns:
    level_one = base[base["Industry_aggregation_NZSIOC"].astype(str).str.lower().eq("level 1")].copy()
    if len(level_one):
        base = level_one

base["Variable_name"] = base["Variable_name"].astype(str)
base["_variable_lower"] = base["Variable_name"].str.lower()
unit_label = str(base["Units"].iloc[0]) if "Units" in base.columns and len(base) else "Value"

if ("income" in q and ("expenditure" in q or "expense" in q)) or "trend" in q:
    selected = base[base["_variable_lower"].isin(["total income", "total expenditure"])].copy()
    selected = selected[selected["_year_num"].notna()].copy()
    if len(selected):
        trend = selected.pivot_table(index="_year_num", columns="Variable_name", values="_value_num", aggfunc="sum").reset_index()
        trend = trend.sort_values("_year_num").rename(columns={"_year_num": "Year"})
        fig, ax = plt.subplots(figsize=(9, 5))
        for column in trend.columns:
            if column != "Year":
                ax.plot(trend["Year"], trend[column], marker="o", linewidth=2, label=str(column))
        ax.set_title("Total income vs total expenditure")
        ax.set_xlabel("Year")
        ax.set_ylabel(unit_label)
        ax.legend()
        ax.grid(True, alpha=0.3)
        chart_path = OUTPUT_DIR / "income_vs_expenditure.png"
        plt.tight_layout()
        plt.savefig(chart_path, bbox_inches="tight")
        emit_chart(chart_path, "Total income vs total expenditure")
        emit_table("Income vs expenditure trend", trend)
        latest_year = int(trend["Year"].max()) if len(trend) else None
        latest = trend[trend["Year"].eq(latest_year)].iloc[0] if latest_year is not None else None
        if latest is not None and "Total income" in trend.columns and "Total expenditure" in trend.columns:
            emit_insight(f"I plotted Total income and Total expenditure by year. In {latest_year}, Total income was {latest['Total income']:,.0f} and Total expenditure was {latest['Total expenditure']:,.0f}. Units: {unit_label}.")
        else:
            emit_insight(f"I plotted Total income and Total expenditure by year. Units: {unit_label}.")
    else:
        emit_insight("I could not find numeric Total income and Total expenditure rows to plot.")
else:
    latest_year = int(base["_year_num"].max()) if base["_year_num"].notna().any() else None
    latest = base[base["_year_num"].eq(latest_year)].copy() if latest_year is not None else base.copy()
    if "Variable_category" in latest.columns and "performance" in q:
        latest = latest[latest["Variable_category"].astype(str).str.lower().eq("financial performance")].copy()
    top = latest.sort_values("_value_num", ascending=False).head(10)
    fig, ax = plt.subplots(figsize=(9, 5))
    ax.barh(top["Variable_name"].astype(str), top["_value_num"])
    ax.invert_yaxis()
    ax.set_title("Top metrics by value")
    ax.set_xlabel(unit_label)
    chart_path = OUTPUT_DIR / "top_metrics.png"
    plt.tight_layout()
    plt.savefig(chart_path, bbox_inches="tight")
    emit_chart(chart_path, "Top metrics by value")
    emit_table("Top metrics by value", top[["Variable_name", "Variable_category", "_value_num"]].rename(columns={"_value_num": "Value"}))
    year_text = f" for {latest_year}" if latest_year is not None else ""
    emit_insight(f"I plotted the top metrics by value{year_text}. Units: {unit_label}.")
""".strip()


FALLBACK_PROFILE_CODE = """
rows, cols = df.shape
missing_total = int(df.isna().sum().sum())
numeric_cols = list(df.select_dtypes(include=np.number).columns)
emit_insight(f"The dataset has {rows:,} rows and {cols:,} columns. It contains {missing_total:,} missing values. Numeric columns: {', '.join(map(str, numeric_cols[:12])) or 'none detected'}.")
emit_table("Sample rows", df.head(10))
if numeric_cols:
    emit_table("Numeric summary", df[numeric_cols].describe().reset_index())
""".strip()


GENERIC_ANALYSIS_HELPERS = """
q = QUESTION.lower()

def _name_l(col):
    return str(col).strip().lower().replace("_", " ").replace("-", " ")

def _clean_text(value):
    text = str(value).strip().lower().replace("_", " ").replace("-", " ")
    for char in [",", ".", "?", "!", ":", ";", "(", ")", "[", "]", "{", "}", "/", "\\\\"]:
        text = text.replace(char, " ")
    return " ".join(text.split())

def _singular(value):
    text = _clean_text(value)
    if text.endswith("ies"):
        return f"{text[:-3]}y"
    if text.endswith("s") and not text.endswith("ss"):
        return text[:-1]
    return text

def _query_has(term):
    term = _clean_text(term)
    query = f" {_clean_text(q)} "
    singular = _singular(term)
    return f" {term} " in query or f" {singular} " in query

def _query_tokens():
    return _clean_text(q).split()

def _requested_count(default=10):
    words = {
        "one": 1,
        "two": 2,
        "three": 3,
        "four": 4,
        "five": 5,
        "six": 6,
        "seven": 7,
        "eight": 8,
        "nine": 9,
        "ten": 10,
    }
    tokens = _query_tokens()
    for token in tokens:
        if token.isdigit():
            value = int(token)
            if 0 < value <= 250:
                return value
        if token in words:
            return words[token]
    return default

def _has_any(col, words):
    name = _name_l(col)
    return any(_clean_text(word) in name for word in words)

def _column_alias_score(col, aliases):
    name = _name_l(col)
    score = 0
    for alias in aliases:
        cleaned = _clean_text(alias)
        if _query_has(cleaned) and (cleaned in name or _singular(cleaned) in name):
            score += 550
    if ("product" in q or "products" in q) and name in ["item type", "item", "product", "product name", "category"]:
        score += 650
    if ("country" in q or "countries" in q) and name == "country":
        score += 700
    if ("region" in q or "regions" in q) and name == "region":
        score += 700
    if ("channel" in q or "channels" in q) and "channel" in name:
        score += 650
    return score

def _best_column_from_question(include_numeric=True, include_text=True):
    candidates = []
    for col in df.columns:
        is_numeric = pd.api.types.is_numeric_dtype(df[col])
        if (is_numeric and not include_numeric) or ((not is_numeric) and not include_text):
            continue
        name = _name_l(col)
        score = _column_alias_score(col, [name])
        if name in _clean_text(q):
            score += 500
        for token in name.split():
            if len(token) > 2 and _query_has(token):
                score += 80
        if score > 0:
            candidates.append((score, str(col), col))
    if not candidates:
        return None
    return sorted(candidates, reverse=True)[0][2]

def _mentioned_text_col():
    return _best_column_from_question(include_numeric=False, include_text=True)

def _mentioned_numeric_col():
    return _best_column_from_question(include_numeric=True, include_text=False)

def _requested_metric():
    explicit = _mentioned_numeric_col()
    if explicit is not None and not _is_identifier_col(explicit) and not _is_time_col(explicit):
        return explicit, _numeric_series(explicit)
    if "profit" in q:
        return _best_metric(include_words=["total profit", "profit", "margin"])
    if "cost" in q:
        return _best_metric(include_words=["total cost", "cost"])
    if "price" in q:
        return _best_metric(include_words=["unit price", "total price", "price"])
    if "unit" in q or "units" in q or "quantity" in q:
        return _best_metric(include_words=["units sold", "unit", "quantity", "qty"])
    if "revenue" in q or "sales" in q or "amount" in q:
        return _best_metric(include_words=["total revenue", "revenue", "sales", "amount"])
    return _best_metric()

def _requested_group_col():
    explicit = _mentioned_text_col()
    if explicit is not None:
        return explicit
    return _best_group_col()

def _aggregation_kind():
    tokens = _query_tokens()
    clean = _clean_text(q)
    if any(word in tokens for word in ["average", "avg", "mean"]):
        return "mean"
    if "how many" in clean or "number of" in clean or "count" in tokens:
        return "count"
    if any(word in tokens for word in ["minimum", "min", "lowest", "smallest", "cheapest"]):
        return "min"
    if any(word in tokens for word in ["maximum", "max", "highest", "largest", "top", "best"]):
        return "sum"
    return "sum"

def _apply_mentioned_filters(frame):
    filtered = frame
    ignored_cols = []
    group_col = _mentioned_text_col()
    sort_col = _best_column_from_question(include_numeric=True, include_text=True)
    if group_col is not None:
        ignored_cols.append(_name_l(group_col))
    if sort_col is not None:
        ignored_cols.append(_name_l(sort_col))
    for col in df.select_dtypes(exclude=np.number).columns:
        if _name_l(col) in ignored_cols:
            continue
        values = df[col].dropna().astype(str).drop_duplicates()
        matches = []
        for value in values:
            cleaned = _clean_text(value)
            if len(cleaned) >= 3 and f" {cleaned} " in f" {_clean_text(q)} ":
                matches.append(value)
        if matches:
            filtered = filtered[filtered[col].astype(str).isin(matches)]
    return filtered

def _total_label(col):
    text = str(col)
    if _name_l(col).startswith("total "):
        return text
    return f"total {text}"

def _is_identifier_col(col):
    name = _name_l(col)
    padded = f" {name} "
    return (
        name in ["id", "row id", "record id", "order id", "customer id", "product id", "user id"]
        or name.endswith(" id")
        or " id " in padded
        or name.endswith(" code")
        or " code " in padded
        or "postal" in name
        or "zip" in name
        or "phone" in name
        or "rank" in name
        or "serial" in name
        or "invoice number" in name
        or "order number" in name
    )

def _is_time_col(col):
    name = _name_l(col)
    return any(word in name for word in ["date", "year", "month", "quarter", "week", "day", "time", "timestamp", "period", "created", "updated"])

def _numeric_series(col):
    series = df[col]
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    text = series.astype(str)
    text = text.str.replace(",", "", regex=False)
    text = text.str.replace("$", "", regex=False)
    text = text.str.replace("%", "", regex=False)
    text = text.str.replace("(", "-", regex=False)
    text = text.str.replace(")", "", regex=False)
    text = text.str.strip()
    return pd.to_numeric(text, errors="coerce")

def _numeric_candidates():
    candidates = []
    row_count = max(len(df), 1)
    for col in df.columns:
        values = _numeric_series(col)
        valid = int(values.notna().sum())
        if valid == 0:
            continue
        if valid / row_count >= 0.65:
            candidates.append({"column": col, "values": values, "valid": valid})
    return candidates

def _metric_score(col, values):
    name = _name_l(col)
    score = 0
    if _is_identifier_col(col):
        score -= 1000
    if _is_time_col(col):
        score -= 800
    if "total revenue" in name or "total sales" in name or "sales amount" in name:
        score += 260
    elif "revenue" in name or "sales" in name:
        score += 230
    if "amount" in name or "value" in name or "net sales" in name:
        score += 170
    if "total profit" in name or name == "profit":
        score += 130
    elif "profit" in name or "margin" in name:
        score += 105
    if "total cost" in name:
        score += 125
    elif "cost" in name:
        score += 85
    if "total price" in name:
        score += 150
    elif "price" in name:
        score += 70
    if "unit price" in name or "unit cost" in name:
        score -= 35
    if "units sold" in name or name == "units":
        score += 45
    if "sales" in q:
        if "total revenue" in name or "total sales" in name:
            score += 220
        elif "revenue" in name or "sales" in name:
            score += 190
        if "cost" in name or "unit price" in name or "unit cost" in name:
            score -= 90
    if "profit" in q and "profit" in name:
        score += 220
    if "cost" in q and "cost" in name:
        score += 210
    if ("price" in q or "revenue" in q) and ("revenue" in name or "price" in name or "sales" in name):
        score += 130
    non_null = values.dropna()
    if len(non_null):
        unique_ratio = non_null.nunique() / max(len(non_null), 1)
        if unique_ratio > 0.9 and _is_identifier_col(col):
            score -= 250
    return score

def _best_metric(include_words=None, exclude_cols=None):
    include_words = include_words or []
    exclude_cols = exclude_cols or []
    excluded_names = {_name_l(col) for col in exclude_cols}
    candidates = []
    for item in _numeric_candidates():
        col = item["column"]
        if _name_l(col) in excluded_names:
            continue
        if include_words and not _has_any(col, include_words):
            continue
        score = _metric_score(col, item["values"])
        candidates.append((score, str(col), col, item["values"]))
    if not candidates and include_words:
        return _best_metric(exclude_cols=exclude_cols)
    if not candidates:
        return None, None
    candidates = sorted(candidates, reverse=True)
    return candidates[0][2], candidates[0][3]

def _best_date_col():
    candidates = []
    for col in df.columns:
        if _is_identifier_col(col):
            continue
        if not _has_any(col, ["date", "year", "month", "time", "timestamp", "period", "created", "updated"]):
            continue
        if pd.api.types.is_numeric_dtype(df[col]) and not _has_any(col, ["date", "year", "month"]):
            continue
        converted = pd.to_datetime(df[col], errors="coerce")
        ratio = converted.notna().mean()
        if ratio > 0.65:
            score = 0
            if "order date" in _name_l(col):
                score += 160
            elif "date" in _name_l(col):
                score += 120
            if "ship" in _name_l(col):
                score -= 25
            if "year" in _name_l(col):
                score += 50
            candidates.append((score, str(col), col))
    if not candidates:
        return None
    candidates = sorted(candidates, reverse=True)
    return candidates[0][2]

def _best_group_col():
    text_cols = list(df.select_dtypes(exclude=np.number).columns)
    candidates = []
    for col in text_cols:
        name = _name_l(col)
        if _is_identifier_col(col) or _is_time_col(col):
            continue
        unique_count = int(df[col].nunique(dropna=True))
        score = 0
        score += _column_alias_score(col, [name])
        if unique_count < 2 or (unique_count > max(60, len(df) * 0.75) and score < 500):
            continue
        if name in _clean_text(q):
            score += 300
        if "region" in name:
            score += 110
        if "country" in name:
            score += 95
        if any(word in name for word in ["customer", "client", "salesperson", "sales person", "rep", "seller"]):
            score += 100
        if any(word in name for word in ["product", "item", "category", "channel", "segment"]):
            score += 75
        score -= unique_count * 0.05
        candidates.append((score, str(col), col))
    if not candidates:
        return None
    candidates = sorted(candidates, reverse=True)
    return candidates[0][2]
""".strip()


FALLBACK_TOP_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

metric, metric_values = _best_metric()
group_col = _best_group_col()
asks_ranking = any(word in q for word in ["top", "highest", "best", "largest", "who", " by "])
asks_total_only = any(word in q for word in ["total", "sum"]) and not asks_ranking
if metric is None:
    emit_insight("I could not find a usable numeric business measure to rank or aggregate.")
    emit_table("Sample rows", df.head(10))
elif asks_total_only:
    total_value = float(metric_values.sum())
    table = pd.DataFrame([{"Metric": str(metric), "Total": total_value}])
    label = _total_label(metric)
    emit_insight(f"{label} is {total_value:,.2f}.")
    emit_table(label, table)
else:
    if group_col:
        work = df[[group_col]].copy()
        work[metric] = metric_values
        table = work.groupby(group_col, dropna=False)[metric].sum().sort_values(ascending=False).head(10).reset_index()
        leader = table.iloc[0]
        emit_insight(f"Highest sales: {leader[group_col]} with total {metric} of {leader[metric]:,.2f}.")
        emit_table(f"Top {group_col} by {metric}", table)
    else:
        work = df.copy()
        work[metric] = metric_values
        table = work.sort_values(metric, ascending=False).head(10)
        emit_insight(f"Top 10 rows by {metric}.")
        emit_table(f"Top rows by {metric}", table)
""".strip()


FALLBACK_LIST_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

target_col = _best_column_from_question(include_numeric=False, include_text=True)
if target_col is None:
    emit_insight("I could not identify which text column to list. Available columns are shown below.")
    emit_table("Columns", pd.DataFrame({"Column": [str(col) for col in df.columns]}))
else:
    values = df[target_col].dropna().astype(str).drop_duplicates().sort_values().reset_index(drop=True)
    table = pd.DataFrame({str(target_col): values})
    emit_insight(f"Found {len(table):,} unique {target_col} values.")
    emit_table(f"Unique {target_col}", table, max_rows=250)
""".strip()


FALLBACK_SORT_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

sort_col = _best_column_from_question(include_numeric=True, include_text=True)
if sort_col is None:
    emit_insight("I could not identify which column to sort by. Available columns are shown below.")
    emit_table("Columns", pd.DataFrame({"Column": [str(col) for col in df.columns]}))
else:
    ascending = not any(word in q for word in ["descending", "desc", "highest", "largest", "decreasing"])
    work = df.copy()
    numeric_values = _numeric_series(sort_col)
    if numeric_values.notna().mean() >= 0.65:
        work["_sort_key"] = numeric_values
        sorted_rows = work.sort_values("_sort_key", ascending=ascending, na_position="last")
        sorted_rows = sorted_rows[[col for col in sorted_rows.columns if col != "_sort_key"]]
    else:
        sorted_rows = work.sort_values(sort_col, ascending=ascending, na_position="last")
    direction = "ascending" if ascending else "descending"
    emit_insight(f"Sorted rows by {sort_col} in {direction} order.")
    emit_table(f"Rows sorted by {sort_col} ({direction})", sorted_rows, max_rows=100)
""".strip()


FALLBACK_SMART_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

clean_q = _clean_text(q)
sort_intent = any(word in clean_q.split() for word in ["sort", "ascending", "descending", "asc", "desc"]) or "order by" in clean_q
ranking_intent = any(word in clean_q.split() for word in ["top", "highest", "best", "largest", "lowest", "smallest", "cheapest"])
total_intent = any(phrase in clean_q for phrase in ["total", "sum", "how much"])
average_intent = any(word in clean_q.split() for word in ["average", "avg", "mean"])
count_intent = "how many" in clean_q or any(word in clean_q.split() for word in ["count", "number"])
by_intent = " by " in f" {clean_q} "
display_list_intent = any(word in clean_q.split() for word in ["show", "display"]) and _mentioned_text_col() is not None and not any(word in clean_q.split() for word in ["revenue", "sales", "profit", "cost", "price", "total", "top", "highest", "lowest", "average", "avg", "mean", "sort"])
list_intent = any(word in clean_q.split() for word in ["list", "unique", "distinct"]) or display_list_intent

if sort_intent:
    sort_col = _best_column_from_question(include_numeric=True, include_text=True)
    if sort_col is None:
        emit_insight("I could not identify which column to sort by. Available columns are shown below.")
        emit_table("Columns", pd.DataFrame({"Column": [str(col) for col in df.columns]}))
    else:
        ascending = not any(word in clean_q.split() for word in ["descending", "desc", "highest", "largest", "decreasing"])
        work = _apply_mentioned_filters(df.copy())
        numeric_values = _numeric_series(sort_col).loc[work.index]
        if numeric_values.notna().mean() >= 0.65:
            work["_sort_key"] = numeric_values
            sorted_rows = work.sort_values("_sort_key", ascending=ascending, na_position="last")
            sorted_rows = sorted_rows[[col for col in sorted_rows.columns if col != "_sort_key"]]
        else:
            sorted_rows = work.sort_values(sort_col, ascending=ascending, na_position="last")
        direction = "ascending" if ascending else "descending"
        emit_insight(f"Sorted {len(sorted_rows):,} rows by {sort_col} in {direction} order.")
        emit_table(f"Rows sorted by {sort_col} ({direction})", sorted_rows, max_rows=100)
elif list_intent:
    target_col = _mentioned_text_col() or _mentioned_numeric_col()
    if target_col is None:
        emit_insight("I could not identify which column to list. Available columns are shown below.")
        emit_table("Columns", pd.DataFrame({"Column": [str(col) for col in df.columns]}))
    else:
        values = df[target_col].dropna().drop_duplicates().sort_values().reset_index(drop=True)
        table = pd.DataFrame({str(target_col): values})
        emit_insight(f"Found {len(table):,} unique {target_col} values.")
        emit_table(f"Unique {target_col}", table, max_rows=250)
elif count_intent and not total_intent and not average_intent:
    group_col = _mentioned_text_col()
    work = _apply_mentioned_filters(df.copy())
    if group_col is not None:
        table = work.groupby(group_col, dropna=False).size().reset_index(name="Count").sort_values("Count", ascending=False)
        emit_insight(f"Count by {group_col}. Top value: {table.iloc[0][group_col]} with {int(table.iloc[0]['Count']):,} rows.")
        emit_table(f"Count by {group_col}", table, max_rows=100)
    else:
        emit_insight(f"The filtered dataset contains {len(work):,} rows.")
        emit_table("Row count", pd.DataFrame([{"Rows": int(len(work))}]))
else:
    metric, metric_values = _requested_metric()
    explicit_group = _mentioned_text_col()
    group_col = explicit_group if by_intent or explicit_group is not None else None
    if ranking_intent and group_col is None:
        group_col = _best_group_col()
    work = _apply_mentioned_filters(df.copy())
    if metric is None:
        rows, cols = work.shape
        missing_total = int(work.isna().sum().sum())
        numeric_cols = list(work.select_dtypes(include=np.number).columns)
        emit_insight(f"The dataset has {rows:,} matching rows and {cols:,} columns. It contains {missing_total:,} missing values. Numeric columns: {', '.join(map(str, numeric_cols[:12])) or 'none detected'}.")
        emit_table("Sample rows", work.head(10))
        if numeric_cols:
            emit_table("Numeric summary", work[numeric_cols].describe().reset_index())
    else:
        values = metric_values.loc[work.index]
        agg = _aggregation_kind()
        ascending = any(word in clean_q.split() for word in ["lowest", "smallest", "cheapest", "min", "minimum"])
        limit = _requested_count()
        if group_col is not None and group_col in work.columns:
            grouped_source = work[[group_col]].copy()
            grouped_source[metric] = values
            if agg == "mean" or average_intent:
                table = grouped_source.groupby(group_col, dropna=False)[metric].mean().sort_values(ascending=ascending).head(limit).reset_index()
                label = f"Average {metric}"
                table = table.rename(columns={metric: label})
            elif agg == "count":
                table = grouped_source.groupby(group_col, dropna=False)[metric].count().sort_values(ascending=ascending).head(limit).reset_index()
                label = "Count"
                table = table.rename(columns={metric: label})
            elif agg == "min":
                table = grouped_source.groupby(group_col, dropna=False)[metric].min().sort_values(ascending=True).head(limit).reset_index()
                label = f"Minimum {metric}"
                table = table.rename(columns={metric: label})
            else:
                table = grouped_source.groupby(group_col, dropna=False)[metric].sum().sort_values(ascending=ascending).head(limit).reset_index()
                label = _total_label(metric)
                table = table.rename(columns={metric: label})
            leader = table.iloc[0]
            rank_word = "Lowest" if ascending else "Highest"
            emit_insight(f"{rank_word} {label}: {leader[group_col]} with {leader[label]:,.2f}.")
            emit_table(f"Top {group_col} by {label}", table, max_rows=limit)
        elif total_intent or average_intent or count_intent:
            if average_intent:
                value = float(values.mean())
                label = f"Average {metric}"
            elif count_intent:
                value = int(values.notna().sum())
                label = f"Count of {metric}"
            else:
                value = float(values.sum())
                label = _total_label(metric)
            emit_insight(f"{label} is {value:,.2f}.")
            emit_table(label, pd.DataFrame([{"Metric": str(metric), "Value": value}]))
        elif ranking_intent:
            sorted_rows = work.copy()
            sorted_rows[metric] = values
            sorted_rows = sorted_rows.sort_values(metric, ascending=ascending, na_position="last").head(limit)
            direction = "lowest" if ascending else "highest"
            emit_insight(f"Showing the {limit} {direction} rows by {metric}.")
            emit_table(f"Top rows by {metric}", sorted_rows, max_rows=limit)
        else:
            rows, cols = work.shape
            missing_total = int(work.isna().sum().sum())
            numeric_cols = list(work.select_dtypes(include=np.number).columns)
            emit_insight(f"The dataset has {rows:,} matching rows and {cols:,} columns. It contains {missing_total:,} missing values. Numeric columns: {', '.join(map(str, numeric_cols[:12])) or 'none detected'}.")
            emit_table("Sample rows", work.head(10))
            if numeric_cols:
                emit_table("Numeric summary", work[numeric_cols].describe().reset_index())
""".strip()


FALLBACK_YEAR_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

date_col = _best_date_col()
metric, metric_values = _best_metric()
if metric is None:
    emit_insight("I could not find a usable sales or revenue measure for year-over-year analysis.")
    emit_table("Sample rows", df.head(10))
elif date_col:
    temp = df[[date_col]].copy()
    temp[metric] = metric_values
    temp[date_col] = pd.to_datetime(temp[date_col], errors="coerce")
    temp = temp.dropna(subset=[date_col, metric])
    temp["Year"] = temp[date_col].dt.year.astype(int)
    table = temp.groupby("Year", dropna=False)[metric].sum().reset_index().sort_values(metric, ascending=False)
    emit_insight(f"Year-over-year sales by total {metric}, sorted descending. Best year: {int(table.iloc[0]['Year'])} with {table.iloc[0][metric]:,.2f}.")
    emit_table(f"Year-over-year {metric}", table)
else:
    emit_insight("I found a sales measure, but no date column suitable for year-over-year analysis.")
    emit_table("Sample rows", df.head(10))
""".strip()


FALLBACK_COMPARE_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

cost_metric, cost_values = _best_metric(include_words=["cost"])
price_metric, price_values = _best_metric(include_words=["total revenue", "total price", "revenue", "sales", "price"], exclude_cols=[cost_metric] if cost_metric is not None else [])
if cost_metric is None or price_metric is None:
    emit_insight("I could not find both a cost column and a price/revenue column to compare.")
    emit_table("Sample rows", df.head(10))
else:
    cost_total = float(cost_values.sum())
    price_total = float(price_values.sum())
    difference = price_total - cost_total
    cost_label = _total_label(cost_metric)
    price_label = _total_label(price_metric)
    table = pd.DataFrame([
        {"Metric": str(price_metric), "Total": price_total},
        {"Metric": str(cost_metric), "Total": cost_total},
        {"Metric": f"{price_metric} minus {cost_metric}", "Total": difference},
    ])
    emit_insight(f"{price_label} is {price_total:,.2f}; {cost_label} is {cost_total:,.2f}; difference is {difference:,.2f}.")
    emit_table(f"{price_metric} vs {cost_metric}", table)
""".strip()


FALLBACK_CHART_CODE = GENERIC_ANALYSIS_HELPERS + "\n" + """

date_col = _best_date_col()
metric, metric_values = _best_metric()
requested = []
if "revenue" in q or "sales" in q:
    revenue_metric, revenue_values = _best_metric(include_words=["total revenue", "revenue", "sales"])
    if revenue_metric is not None:
        requested.append((revenue_metric, revenue_values))
if "cost" in q:
    cost_metric, cost_values = _best_metric(include_words=["total cost", "cost"])
    if cost_metric is not None and all(_name_l(cost_metric) != _name_l(item[0]) for item in requested):
        requested.append((cost_metric, cost_values))
if "profit" in q or "margin" in q:
    profit_metric, profit_values = _best_metric(include_words=["total profit", "profit", "margin"])
    if profit_metric is not None and all(_name_l(profit_metric) != _name_l(item[0]) for item in requested):
        requested.append((profit_metric, profit_values))

if len(requested) >= 2:
    if date_col:
        temp = df[[date_col]].copy()
        temp[date_col] = pd.to_datetime(temp[date_col], errors="coerce")
        for column, values in requested:
            temp[column] = values
        temp = temp.dropna(subset=[date_col])
        monthly = temp.set_index(date_col).resample("ME")[[column for column, values in requested]].sum().reset_index()
        fig, ax = plt.subplots(figsize=(9, 4.8))
        for column, values in requested:
            ax.plot(monthly[date_col], monthly[column], marker="o", linewidth=2, label=str(column))
        ax.set_title("Monthly totals")
        ax.set_xlabel(str(date_col))
        ax.set_ylabel("Total")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.autofmt_xdate()
        chart_path = OUTPUT_DIR / "monthly_metric_totals.png"
        plt.tight_layout()
        plt.savefig(chart_path, bbox_inches="tight")
        emit_chart(chart_path, "Monthly revenue, cost, and profit")
        emit_table("Monthly metric totals", monthly.tail(24))
        totals = {str(column): float(values.sum()) for column, values in requested}
        summary = "; ".join(f"total {name}: {value:,.2f}" for name, value in totals.items())
        emit_insight(f"I plotted the requested metrics over time. Overall {summary}.")
    else:
        totals = pd.DataFrame([{"Metric": str(column), "Total": float(values.sum())} for column, values in requested])
        fig, ax = plt.subplots(figsize=(8, 4.8))
        ax.bar(totals["Metric"], totals["Total"])
        ax.set_title("Metric totals")
        ax.set_ylabel("Total")
        ax.tick_params(axis="x", rotation=20)
        chart_path = OUTPUT_DIR / "metric_totals.png"
        plt.tight_layout()
        plt.savefig(chart_path, bbox_inches="tight")
        emit_chart(chart_path, "Revenue, cost, and profit totals")
        emit_table("Metric totals", totals)
        summary = "; ".join(f"{row['Metric']}: {row['Total']:,.2f}" for index, row in totals.iterrows())
        emit_insight(f"I plotted the requested metric totals. {summary}.")
elif metric is not None and date_col:
    temp = df[[date_col]].copy()
    temp[metric] = metric_values
    temp = temp.dropna(subset=[metric])
    temp[date_col] = pd.to_datetime(temp[date_col], errors="coerce")
    temp = temp.dropna(subset=[date_col])
    temp = temp.set_index(date_col).resample("ME")[metric].sum().reset_index()
    fig, ax = plt.subplots(figsize=(9, 4.8))
    ax.plot(temp[date_col], temp[metric], marker="o", linewidth=2)
    ax.set_title(f"Monthly {metric} trend")
    ax.set_xlabel(str(date_col))
    ax.set_ylabel(str(metric))
    fig.autofmt_xdate()
    chart_path = OUTPUT_DIR / "monthly_trend.png"
    plt.tight_layout()
    plt.savefig(chart_path, bbox_inches="tight")
    emit_chart(chart_path, f"Monthly {metric} trend")
    emit_table("Monthly trend data", temp.tail(24))
    emit_insight(f"I plotted monthly {metric} using {date_col}.")
elif metric is not None:
    fig, ax = plt.subplots(figsize=(8, 4.8))
    metric_values.dropna().hist(ax=ax, bins=20)
    ax.set_title(f"Distribution of {metric}")
    ax.set_xlabel(str(metric))
    ax.set_ylabel("Count")
    chart_path = OUTPUT_DIR / "distribution.png"
    plt.tight_layout()
    plt.savefig(chart_path, bbox_inches="tight")
    emit_chart(chart_path, f"Distribution of {metric}")
    emit_insight(f"I plotted the distribution of {metric}.")
else:
    emit_insight("I could not find numeric or date columns suitable for a chart.")
    emit_table("Sample rows", df.head(10))
""".strip()
