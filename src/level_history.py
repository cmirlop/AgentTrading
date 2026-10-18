"""Inventario cronológico de contactos con una zona estructural.

Fase 2.1 del LevelHistoryAnalyzer: analiza el intervalo de precios ACTUAL
contra velas M15 ya cerradas, separando contactos retrospectivos anteriores
a first_detected_at de contactos posteriores a su detección.

No interpreta un contacto como rechazo, ruptura o señal de trading.
No añade información futura al replay; NO afirma que la misma zona fuera
conocida en los instantes históricos de los contactos retrospectivos.

Ejemplo futuro::

    history = analyze_zone_contacts(zone, prepared_history["M15"], analysis_time)

Requiere pandas, ya utilizado por AgentTrading.
"""

from __future__ import annotations

import pandas as pd


def _utc(value) -> pd.Timestamp:
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        raise ValueError("INVALID_TIMESTAMP")
    if ts.tzinfo is None:
        raise ValueError("TIMESTAMP_MUST_HAVE_TIMEZONE")
    return ts.tz_convert("UTC")


def analyze_zone_contacts(
    zone: dict,
    m15: pd.DataFrame,
    analysis_time,
    separation_bars: int = 4,
) -> dict:
    """Devuelve fechas de INICIO de contactos independientes en M15.

    Un nuevo contacto exige al menos `separation_bars` velas cerradas
    consecutivas sin intersectar la zona desde el contacto anterior.
    Los primeros contactos con historial incompleto se omiten.

    IMPORTANTE: se usa el intervalo actual de la zona para explorar el
    pasado; el resultado 'before_detection' es retrospectivo, NO prueba
    que esa zona se conociera entonces.
    """
    if separation_bars < 1:
        raise ValueError("SEPARATION_BARS_MUST_BE_POSITIVE")
    if not zone.get("zone_id") or not zone.get("first_detected_at"):
        raise ValueError("ZONE_REQUIRES_REGISTERED_ID_AND_FIRST_DETECTED_AT")

    low, high = float(zone["low"]), float(zone["high"])
    if not low < high:
        raise ValueError("INVALID_ZONE_INTERVAL")

    moment = _utc(analysis_time)
    first_detected_at = _utc(zone["first_detected_at"])
    if first_detected_at > moment:
        raise ValueError("ZONE_DETECTED_AFTER_ANALYSIS_TIME")

    required = {"bar_close", "low", "high"}
    missing = required.difference(m15.columns)
    if missing:
        raise ValueError(f"M15_MISSING_COLUMNS: {sorted(missing)}")

    # IMPORTANTE: prepared_history contiene a menudo barras posteriores
    # al momento del replay. Nunca se pueden utilizar aqui.
    history = m15.loc[m15["bar_close"] <= moment, ["bar_close", "low", "high"]]
    history = (
        history.sort_values("bar_close")
        .drop_duplicates("bar_close", keep="last")
    )

    before, since = [], []
    bars_outside = 0  # No inventar separacion al principio del dataset.

    for row in history.itertuples(index=False):
        intersects = float(row.high) >= low and float(row.low) <= high
        if intersects:
            if bars_outside >= separation_bars:
                when = _utc(row.bar_close).isoformat()
                if _utc(row.bar_close) < first_detected_at:
                    before.append(when)
                else:
                    since.append(when)
            bars_outside = 0
        else:
            bars_outside = min(separation_bars, bars_outside + 1)

    return {
        "zone_id": zone["zone_id"],
        "analysis_time": moment.isoformat(),
        "first_detected_at": first_detected_at.isoformat(),
        "bounds_used": {"low": low, "high": high},
        "scope": "RETROSPECTIVE_PRICE_ZONE_CONTACTS_M15",
        "separation_bars": separation_bars,
        "closed_bars_examined": int(len(history)),
        "before_detection": {
            "contact_count": len(before),
            "contact_times": before,
        },
        "since_detection": {
            "contact_count": len(since),
            "contact_times": since,
        },
        "limitations": [
            "CONTACT_NOT_CONFIRMED_REJECTION_OR_BREAK",
            "HISTORICAL_ZONE_BOUNDS_ARE_RETROSPECTIVE",
            "INCOMPLETE_CONTACT_AT_START_OF_HISTORY_EXCLUDED",
        ],
    }


def analyze_zone_events(
    zone: dict,
    m15: pd.DataFrame,
    analysis_time,
    separation_bars: int = 4,
    confirmation_closes: int = 2,
    max_resolution_bars: int = 12,
) -> dict:
    """Clasifica episodios de contacto con la franja actual (diagnostico).

    Reglas iniciales, NO optimizadas:
    - nuevo episodio: interseccion despues de 4 velas sin contacto;
    - papel HISTORICO segun el cierre anterior: SUPPORT si venia de
      arriba, RESISTANCE si venia de abajo; en otro caso UNKNOWN;
    - REJECTION: 2 cierres consecutivos fuera por el lado de llegada;
    - BREAK: 2 cierres consecutivos fuera por el lado opuesto;
    - PENDING: no se conoce desenlace a la hora analizada;
    - UNRESOLVED: vencido plazo de 12 velas sin confirmacion.

    Un episodio retrospectivo NO es prueba de que el sistema ya
    hubiera detectado la zona en su fecha. Nunca considera datos
    posteriores a analysis_time. No usar como senal de trading.
    """
    if separation_bars < 1 or confirmation_closes < 1 or max_resolution_bars < 1:
        raise ValueError("INVALID_EPISODE_PARAMETERS")
    if not zone.get("zone_id") or not zone.get("first_detected_at"):
        raise ValueError("ZONE_REQUIRES_REGISTERED_ID_AND_FIRST_DETECTED_AT")

    low = float(zone["low"])
    high = float(zone["high"])
    if not low < high:
        raise ValueError("INVALID_ZONE_INTERVAL")

    now = _utc(analysis_time)
    detected = _utc(zone["first_detected_at"])
    if detected > now:
        raise ValueError("ZONE_DETECTED_AFTER_ANALYSIS_TIME")

    required = {"bar_close", "high", "low", "close"}
    missing = required.difference(m15.columns)
    if missing:
        raise ValueError(f"M15_MISSING_COLUMNS: {sorted(missing)}")

    history = (
        m15.loc[m15["bar_close"] <= now,
                ["bar_close", "high", "low", "close"]]
        .sort_values("bar_close")
        .drop_duplicates("bar_close", keep="last")
    )
    episodes = []
    bars_outside = 0  # no inventar separacion inicial
    previous_close = None
    active = None
    streak_rejection = 0
    streak_break = 0

    for row in history.itertuples(index=False):
        when = _utc(row.bar_close)
        close = float(row.close)
        intersects = float(row.high) >= low and float(row.low) <= high

        if intersects and bars_outside >= separation_bars:
            # Un nuevo contacto independiente cierra el anterior,
            # sin inferir cual habria sido su resultado a posteriori.
            if active is not None and active["outcome"] == "PENDING":
                active["outcome"] = "UNRESOLVED"
                active["resolution_reason"] = "NEW_CONTACT_BEFORE_CONFIRMATION"

            if previous_close is not None and previous_close > high:
                role = "SUPPORT"
            elif previous_close is not None and previous_close < low:
                role = "RESISTANCE"
            else:
                role = "UNKNOWN"

            active = {
                "contact_at": when.isoformat(),
                "phase": "BEFORE_DETECTION" if when < detected else "SINCE_DETECTION",
                "role_at_contact": role,
                "outcome": "PENDING",
                "resolved_at": None,
                "resolution_reason": None,
            }
            episodes.append(active)
            streak_rejection = 0
            streak_break = 0
            age = 0

        if active is not None and active["outcome"] == "PENDING":
            # Cada vela cerrada, incluida la primera, puede contribuir
            # al patron. Cierres dentro de zona rompen la consecutividad.
            role = active["role_at_contact"]
            if role == "SUPPORT":
                rejection = close > high
                broken = close < low
            elif role == "RESISTANCE":
                rejection = close < low
                broken = close > high
            else:
                rejection = broken = False

            streak_rejection = streak_rejection + 1 if rejection else 0
            streak_break = streak_break + 1 if broken else 0
            age += 1

            if streak_rejection >= confirmation_closes:
                active["outcome"] = "REJECTION"
                active["resolved_at"] = when.isoformat()
            elif streak_break >= confirmation_closes:
                active["outcome"] = "BREAK"
                active["resolved_at"] = when.isoformat()
            elif age >= max_resolution_bars:
                active["outcome"] = "UNRESOLVED"
                active["resolution_reason"] = "MAX_RESOLUTION_BARS"

        bars_outside = 0 if intersects else min(separation_bars, bars_outside + 1)
        previous_close = close

    def summary(phase):
        group = [e for e in episodes if e["phase"] == phase]
        return {
            "contact_count": len(group),
            "confirmed_rejections": sum(e["outcome"] == "REJECTION" for e in group),
            "confirmed_breaks": sum(e["outcome"] == "BREAK" for e in group),
            "pending": sum(e["outcome"] == "PENDING" for e in group),
            "unresolved": sum(e["outcome"] == "UNRESOLVED" for e in group),
        }

    return {
        "zone_id": zone["zone_id"],
        "analysis_time": now.isoformat(),
        "bounds_used": {"low": low, "high": high},
        "rules": {
            "separation_bars": separation_bars,
            "confirmation_closes": confirmation_closes,
            "max_resolution_bars": max_resolution_bars,
            "volatility_buffer": "NONE_DIAGNOSTIC_ONLY",
        },
        "before_detection": summary("BEFORE_DETECTION"),
        "since_detection": summary("SINCE_DETECTION"),
        "events": episodes,
        "limitations": [
            "RETROSPECTIVE_BOUNDS_NOT_KNOWN_AT_HISTORICAL_CONTACT",
            "HISTORICAL_ROLE_INFERRED_FROM_PREVIOUS_CLOSE",
            "NOT_A_VALIDATED_TRADING_RULE",
            "NO_ATR_BUFFER_OR_PRICE_ACTION_CONTEXT_YET",
        ],
    }


def _events_self_test() -> None:
    # Ejemplo controlado: 1 rechazo como soporte y despues ruptura.
    origin = pd.Timestamp("2026-04-08T00:00:00Z")
    rows = []
    def bar(lo, hi, cl):
        rows.append({
            "bar_close": origin + pd.Timedelta(minutes=15 * len(rows)),
            "low": lo, "high": hi, "close": cl,
        })

    # 4 velas fuera, contacto, 2 cierres consecutivos arriba (REJECTION).
    for _ in range(4):
        bar(1.11, 1.15, 1.13)
    bar(1.05, 1.14, 1.07)
    bar(1.11, 1.15, 1.13)
    bar(1.11, 1.15, 1.13)
    # 4 velas fuera (las dos anteriores ya cuentan), nuevo contacto.
    bar(1.11, 1.15, 1.13)
    bar(1.11, 1.15, 1.13)
    bar(1.05, 1.14, 1.07)
    bar(0.92, 0.98, 0.95)
    bar(0.92, 0.98, 0.95)
    # 4 velas debajo y contacto DESDE ABAJO: resistencia, sin resolucion.
    for _ in range(4):
        bar(0.92, 0.98, 0.95)
    bar(1.02, 1.12, 1.05)

    df = pd.DataFrame(rows)
    zone = {
        "zone_id": "USDCAD_Z000001",
        "low": 1.0,
        "high": 1.1,
        "first_detected_at": rows[8]["bar_close"].isoformat(),
    }
    all_events = analyze_zone_events(zone, df, rows[-1]["bar_close"])
    assert [e["outcome"] for e in all_events["events"]] == [
        "REJECTION", "BREAK", "PENDING",
    ], all_events
    assert [e["role_at_contact"] for e in all_events["events"]] == [
        "SUPPORT", "SUPPORT", "RESISTANCE",
    ], all_events
    assert all_events["before_detection"]["confirmed_rejections"] == 1
    assert all_events["since_detection"]["confirmed_breaks"] == 1
    assert all_events["since_detection"]["pending"] == 1

    before_confirmation = analyze_zone_events(zone, df, rows[10]["bar_close"])
    assert before_confirmation["events"][1]["outcome"] == "PENDING"
    truncated = df.loc[df["bar_close"] <= rows[10]["bar_close"]].copy()
    assert analyze_zone_events(zone, truncated, rows[10]["bar_close"]) == before_confirmation
    print("OK: rechazo, ruptura, pendiente y ausencia de velas futuras")



def _self_test() -> None:
    start = pd.Timestamp("2026-04-08T00:00:00Z")
    frames = []
    # Separacion >=4 barras; contactos en indices 4, 9 y 14.
    for i in range(15):
        contact = i in (4, 9, 14)
        frames.append({
            "bar_close": start + pd.Timedelta(minutes=15 * (i + 1)),
            "low": 1.01 if contact else 0.90,
            "high": 1.03 if contact else 0.95,
        })
    candles = pd.DataFrame(frames)
    zone = {
        "zone_id": "USDCAD_Z000001",
        "first_detected_at": frames[7]["bar_close"].isoformat(),
        "low": 1.00,
        "high": 1.10,
    }

    # Aunque candles tiene 15 barras, al evaluar la barra 10 solo se
    # pueden ver los contactos 5 (pasado) y 10 (desde deteccion).
    out = analyze_zone_contacts(zone, candles, frames[9]["bar_close"])
    assert out["closed_bars_examined"] == 10, out
    assert out["before_detection"]["contact_count"] == 1, out
    assert out["since_detection"]["contact_count"] == 1, out
    assert out["before_detection"]["contact_times"] == [
        frames[4]["bar_close"].isoformat()
    ], out
    assert out["since_detection"]["contact_times"] == [
        frames[9]["bar_close"].isoformat()
    ], out

    # Avanzar el reloj permite ver el contacto futuro, no antes.
    later = analyze_zone_contacts(zone, candles, frames[14]["bar_close"])
    assert later["since_detection"]["contact_count"] == 2, later

    try:
        analyze_zone_contacts(zone, candles, frames[6]["bar_close"])
    except ValueError as e:
        assert str(e) == "ZONE_DETECTED_AFTER_ANALYSIS_TIME"
    else:
        raise AssertionError("La zona se utilizo antes de su deteccion")

    print("OK: historial M15 separado antes/despues de deteccion y sin velas futuras")
    print("Antes:", out["before_detection"]["contact_times"])
    print("Desde deteccion:", out["since_detection"]["contact_times"])
    _events_self_test()


if __name__ == "__main__":
    _self_test()
