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
        if any(word in q for word in ["plot", "chart", "trend", "graph", "visual"]):
            return FALLBACK_CHART_CODE
        if any(word in q for word in ["top", "highest", "best", "revenue", "sales", "sum", "total"]):
            return FALLBACK_TOP_CODE
        return FALLBACK_PROFILE_CODE


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


FALLBACK_TOP_CODE = """
numeric_cols = list(df.select_dtypes(include=np.number).columns)
text_cols = list(df.select_dtypes(exclude=np.number).columns)
if not numeric_cols:
    emit_insight("I could not find numeric columns to rank or aggregate.")
    emit_table("Sample rows", df.head(10))
else:
    metric = next((c for c in numeric_cols if str(c).lower() in ["revenue", "sales", "amount", "total", "profit"]), numeric_cols[0])
    group_col = text_cols[0] if text_cols else None
    if group_col:
        table = df.groupby(group_col, dropna=False)[metric].sum().sort_values(ascending=False).head(10).reset_index()
        emit_insight(f"Top 10 {group_col} values by total {metric}.")
        emit_table(f"Top {group_col} by {metric}", table)
    else:
        table = df.sort_values(metric, ascending=False).head(10)
        emit_insight(f"Top 10 rows by {metric}.")
        emit_table(f"Top rows by {metric}", table)
""".strip()


FALLBACK_CHART_CODE = """
numeric_cols = list(df.select_dtypes(include=np.number).columns)
date_candidates = []
for col in df.columns:
    converted = pd.to_datetime(df[col], errors="coerce")
    if converted.notna().mean() > 0.65:
        date_candidates.append(col)
if numeric_cols and date_candidates:
    date_col = date_candidates[0]
    metric = next((c for c in numeric_cols if str(c).lower() in ["revenue", "sales", "amount", "total", "profit"]), numeric_cols[0])
    temp = df[[date_col, metric]].copy()
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
elif numeric_cols:
    metric = numeric_cols[0]
    fig, ax = plt.subplots(figsize=(8, 4.8))
    df[metric].dropna().hist(ax=ax, bins=20)
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
