"""Question answering over the forecast ("adaptive, query-based" use of the system).

A language model answers by calling the lookup tools below, so every number it quotes comes
from the database. Gemini is used when GANGA_GEMINI_API_KEY is set, otherwise Claude when
Anthropic credentials are configured. With neither, a simple built-in responder uses the same
lookups for the common questions (a place's outlook, the worst stretch).
"""

import difflib
import json
import re
from datetime import date, timedelta

from ganga.db import connect

MODEL = "claude-opus-5-5"
LIMITS = {
    "do": ("below", 5.0, "mg/L"),
    "bod": ("above", 3.0, "mg/L"),
    "fc": ("above", 2500.0, "MPN/100 mL"),
}
LABEL = {"do": "dissolved oxygen", "bod": "BOD", "fc": "fecal coliform"}

SYSTEM = """You answer questions about water quality in the Ganga between Haridwar and Patna, \
using a forecasting system's data.

Use the tools to look up every number you state; never estimate from memory. Places are \
identified by name; call find_place first if you are unsure of the spelling.

Be accurate about what the numbers are: they are model estimates tuned to monthly laboratory \
measurements, not sensor readings, and each comes with a range. Bathing limits: dissolved \
oxygen at least 5 mg/L, BOD at most 3 mg/L, fecal coliform at most 2500 MPN/100 mL.

Answer in two to five plain sentences. Lead with the direct answer, quote the key values with \
units and the date they apply to, and mention the chance of failing the bathing limits when the \
question is about safety. If the data cannot answer the question, say so."""


def _run():
    return connect()


def _latest(conn):
    row = conn.execute(
        "SELECT id, issued_on FROM forecast_runs WHERE status = 'ok' "
        "ORDER BY issued_on DESC, id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise LookupError("no forecast has been issued yet")
    return row


def find_place(name: str) -> str:
    """Find places on the river matching a name.

    Args:
        name: A town or place name, e.g. "Varanasi" or "Kanpur".
    """
    with _run() as conn:
        rows = conn.execute(
            "SELECT name, chainage_km FROM nodes WHERE kind = 'node' ORDER BY chainage_km"
        ).fetchall()
    names = [r[0] for r in rows]
    low = name.lower().strip()
    hits = [r for r in rows if low in r[0].lower()]
    if not hits:
        close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
        hits = [r for r in rows if r[0] in close]
    if not hits:
        return json.dumps({"matches": [], "known_places": names})
    return json.dumps({"matches": [{"place": n, "km_from_haridwar": round(c)} for n, c in hits]})


def _resolve(conn, place: str) -> tuple[str, float]:
    rows = conn.execute("SELECT name, chainage_km FROM nodes WHERE kind = 'node'").fetchall()
    low = place.lower().strip()
    exact = [r for r in rows if r[0].lower() == low]
    part = [r for r in rows if low in r[0].lower()]
    if exact or part:
        return (exact or part)[0]
    close = difflib.get_close_matches(place, [r[0] for r in rows], n=1, cutoff=0.5)
    if close:
        return next(r for r in rows if r[0] == close[0])
    raise LookupError(f"no place called {place!r}; use find_place to see the options")


def get_forecast(place: str) -> str:
    """Water quality at a place: recent days and the five-day forecast, with ranges.

    Args:
        place: Place name on the river, e.g. "Varanasi upstream (Assi)".
    """
    with _run() as conn:
        run_id, issued = _latest(conn)
        try:
            name, km = _resolve(conn, place)
        except LookupError as exc:
            return json.dumps({"error": str(exc)})
        cell = conn.execute(
            "SELECT chainage_km FROM forecasts WHERE run_id = %s "
            "ORDER BY abs(chainage_km - %s) LIMIT 1",
            (run_id, km),
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT variable, time, p05, p50, p95 FROM forecasts WHERE run_id = %s "
            "AND chainage_km = %s AND variable = ANY(%s) AND time >= %s ORDER BY time",
            (run_id, cell, ["do", "bod", "fc", "flow", "p_unfit"], issued - timedelta(days=3)),
        ).fetchall()
    days: dict[str, dict] = {}
    for variable, t, p05, p50, p95 in rows:
        d = days.setdefault(t.isoformat(), {"date": t.isoformat(), "is_forecast": t > issued})
        if variable == "p_unfit":
            d["chance_of_failing_bathing_limits"] = round(p50, 2)
        else:
            digits = 0 if variable in ("fc", "flow") else 1
            d[variable] = {
                "estimate": round(p50, digits),
                "low": round(p05, digits),
                "high": round(p95, digits),
            }
    return json.dumps(
        {
            "place": name,
            "km_from_haridwar": round(km),
            "issued_on": issued.isoformat(),
            "units": {"do": "mg/L", "bod": "mg/L", "fc": "MPN/100 mL", "flow": "m3/s"},
            "days": list(days.values()),
        }
    )


def worst_places(variable: str = "fc", days_ahead: int = 0) -> str:
    """The places with the worst water quality on a given day.

    Args:
        variable: One of "fc" (fecal coliform), "bod", "do" (dissolved oxygen) or "risk"
            (chance of failing bathing limits).
        days_ahead: 0 for today, up to 5 for the forecast.
    """
    key = {"risk": "p_unfit"}.get(variable, variable)
    if key not in ("fc", "bod", "do", "p_unfit"):
        return json.dumps({"error": "variable must be fc, bod, do or risk"})
    with _run() as conn:
        run_id, issued = _latest(conn)
        day = issued + timedelta(days=max(0, min(int(days_ahead), 5)))
        rows = conn.execute(
            """
            SELECT n.name, n.chainage_km, f.p50 FROM nodes n
            JOIN LATERAL (
                SELECT p50 FROM forecasts WHERE run_id = %s AND variable = %s AND time = %s
                ORDER BY abs(chainage_km - n.chainage_km) LIMIT 1
            ) f ON TRUE WHERE n.kind = 'node'
            """,
            (run_id, key, day),
        ).fetchall()
    rows.sort(key=lambda r: r[2], reverse=key != "do")
    digits = 2 if key == "p_unfit" else (0 if key == "fc" else 1)
    return json.dumps(
        {
            "day": day.isoformat(),
            "variable": variable,
            "worst_first": [
                {"place": n, "km_from_haridwar": round(c), "value": round(v, digits)}
                for n, c, v in rows[:6]
            ],
            "best": [{"place": n, "value": round(v, digits)} for n, _, v in rows[-3:]],
        }
    )


def get_measurements(place: str, variable: str = "fc") -> str:
    """Latest laboratory measurements near a place (monthly monitoring, not real time).

    Args:
        place: Place name on the river.
        variable: One of "fc", "bod", "do".
    """
    if variable not in LIMITS:
        return json.dumps({"error": "variable must be fc, bod or do"})
    with _run() as conn:
        try:
            name, km = _resolve(conn, place)
        except LookupError as exc:
            return json.dumps({"error": str(exc)})
        rows = conn.execute(
            """
            SELECT s.name, o.time, o.value
            FROM obs_best o JOIN stations s ON s.code = o.station_code
            WHERE abs(s.chainage_km - %s) <= 12 AND o.variable = %s
              AND o.resolution IN ('month', 'day') ORDER BY o.time DESC LIMIT 6
            """,
            (km, variable),
        ).fetchall()
    return json.dumps(
        {
            "place": name,
            "variable": variable,
            "unit": LIMITS[variable][2],
            "measurements": [
                {"station": s, "month": t.isoformat()[:7], "value": v} for s, t, v in rows
            ],
        }
    )


def get_alerts() -> str:
    """Alerts raised in the last seven days."""
    with _run() as conn:
        rows = conn.execute(
            "SELECT kind, severity, valid_on, message FROM alerts "
            "WHERE created_at > now() - interval '7 days' "
            "AND NOT coalesce((details->>'repeat')::boolean, false) "
            "ORDER BY created_at DESC LIMIT 12"
        ).fetchall()
    return json.dumps(
        {
            "alerts": [
                {"kind": k, "severity": s, "day": d.isoformat() if d else None, "message": m}
                for k, s, d, m in rows
            ]
        }
    )


TOOLS = (find_place, get_forecast, worst_places, get_measurements, get_alerts)


def ask_claude(question: str) -> dict:
    """Answer with Claude and the lookup tools. Raises if no credentials are configured."""
    import anthropic
    from anthropic import beta_tool

    used: list[str] = []

    def tracked(fn):
        def wrapper(*args, **kwargs):
            used.append(fn.__name__)
            return fn(*args, **kwargs)

        wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
        wrapper.__annotations__ = fn.__annotations__
        wrapper.__wrapped__ = fn
        return beta_tool(wrapper)

    client = anthropic.Anthropic()
    runner = client.beta.messages.tool_runner(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM,
        output_config={"effort": "low"},
        # If a request is declined by a safety classifier, let the API retry it on a
        # fallback model instead of failing.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        tools=[tracked(fn) for fn in TOOLS],
        max_iterations=8,
        messages=[{"role": "user", "content": question}],
    )
    final = runner.until_done()
    if final.stop_reason == "refusal":
        return {"answer": "That question could not be answered.", "used": used, "engine": "claude"}
    text = "".join(b.text for b in final.content if b.type == "text").strip()
    return {"answer": text or "No answer was produced.", "used": used, "engine": "claude"}


GEMINI_FALLBACK_MODEL = "gemini-2.5-flash-lite"


def ask_gemini(question: str, api_key: str, model: str) -> dict:
    """Answer with Gemini and the lookup tools (the SDK runs the tool calls itself)."""
    import functools

    from google import genai
    from google.genai import types

    used: list[str] = []

    def tracked(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            used.append(fn.__name__)
            return fn(*args, **kwargs)

        return wrapper

    # Tool calling is served by the v1beta API.
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        tools=[tracked(fn) for fn in TOOLS],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(maximum_remote_calls=8),
        max_output_tokens=2000,
    )
    last_error: Exception | None = None
    for name in dict.fromkeys((model, GEMINI_FALLBACK_MODEL)):
        try:
            response = client.chats.create(model=name, config=config).send_message(question)
        except Exception as exc:  # unknown model, quota, network: try the next, then give up
            last_error = exc
            used.clear()
            continue
        text = (response.text or "").strip()
        if text:
            return {"answer": text, "used": used, "engine": f"gemini:{name}"}
    raise RuntimeError(f"Gemini did not answer: {last_error!r}")


def _describe(day: dict) -> list[str]:
    parts = []
    for v in ("fc", "bod", "do"):
        if v not in day:
            continue
        side, limit, unit = LIMITS[v]
        x = day[v]["estimate"]
        ok = x >= limit if side == "below" else x <= limit
        num = f"{x:,.0f}" if v == "fc" else f"{x:.1f}"
        parts.append(f"{LABEL[v]} {num} {unit} ({'within' if ok else 'outside'} the limit)")
    return parts


def ask_builtin(question: str) -> dict:
    """Keyword responder used when no language model is configured."""
    q = question.lower()
    used = []
    ahead = 1 if "tomorrow" in q else 0
    m = re.search(r"in (\d) days?", q)
    if m:
        ahead = min(int(m.group(1)), 5)
    if any(w in q for w in ("worst", "most polluted", "dirtiest", "highest")):
        var = "bod" if "bod" in q else "do" if "oxygen" in q else "risk" if "risk" in q else "fc"
        data = json.loads(worst_places(var, ahead))
        used.append("worst_places")
        top = data["worst_first"][:3]
        label = {
            "fc": "fecal coliform",
            "bod": "BOD",
            "do": "dissolved oxygen",
            "risk": "chance of failing bathing limits",
        }[var]
        listing = "; ".join(f"{t['place']} ({t['value']:,})" for t in top)
        return {
            "answer": f"On {data['day']}, the worst places for {label} are: {listing}. "
            "These are model estimates, not sensor readings.",
            "used": used,
            "engine": "builtin",
        }
    with _run() as conn:
        names = [r[0] for r in conn.execute("SELECT name FROM nodes WHERE kind = 'node'")]
    found = [n for n in names if re.split(r"[ (]", n.lower())[0] in q]
    if not found:
        return {
            "answer": 'I can answer about a named place (for example "Is it safe to bathe '
            'at Varanasi tomorrow?") or where the river is worst. Places: '
            + ", ".join(sorted({n.split(" (")[0] for n in names}))
            + ".",
            "used": used,
            "engine": "builtin",
        }
    seen, answers = set(), []
    for name in found:
        town = name.split(" ")[0]
        if town in seen:
            continue
        seen.add(town)
        data = json.loads(get_forecast(name))
        used.append("get_forecast")
        target = (date.fromisoformat(data["issued_on"]) + timedelta(days=ahead)).isoformat()
        day = next((d for d in data["days"] if d["date"] == target), data["days"][-1])
        risk = day.get("chance_of_failing_bathing_limits", 0)
        verdict = "likely fails" if risk >= 0.6 else "may fail" if risk >= 0.25 else "likely meets"
        answers.append(
            f"{data['place']} on {day['date']}: {verdict} the bathing limits "
            f"({risk:.0%} chance of failing). " + "; ".join(_describe(day)) + "."
        )
    return {
        "answer": " ".join(answers) + " These are model estimates, not sensor readings.",
        "used": used,
        "engine": "builtin",
    }


def ask(question: str) -> dict:
    question = question.strip()[:600]
    if not question:
        return {
            "answer": "Ask about a place or about where the river is worst.",
            "used": [],
            "engine": "builtin",
        }
    from ganga.config import settings

    if settings.gemini_api_key:
        try:
            return ask_gemini(question, settings.gemini_api_key, settings.gemini_model)
        except Exception:
            return ask_builtin(question)
    try:
        import anthropic
    except ImportError:
        return ask_builtin(question)
    try:
        return ask_claude(question)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, TypeError):
        return ask_builtin(question)  # no usable credentials
    except anthropic.RateLimitError:
        out = ask_builtin(question)
        out["answer"] += " (The assistant is busy; this is the short answer.)"
        return out
    except anthropic.APIStatusError:
        return ask_builtin(question)
    except anthropic.APIConnectionError:
        return ask_builtin(question)
    except anthropic.AnthropicError:
        return ask_builtin(question)  # e.g. no credentials found when building the client
