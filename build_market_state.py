import json
from pathlib import Path

import pandas as pd

from config.market_config import (
    SYMBOLS,
    TIMEFRAMES,
    RECENT_CANDLES,
    ANALYSIS_TIMEFRAME,
)

from src.indicators import (
    add_indicators,
    get_indicator_snapshot,
    safe_number,
)


# ==========================================================
# CONFIGURACION
# ==========================================================

BASE_DIR = Path(__file__).resolve().parent

MARKET_DIR = (
    BASE_DIR
    / "data"
    / "market"
)

STATE_DIR = (
    BASE_DIR
    / "data"
    / "state"
)


TIMEFRAME_SECONDS = {
    "M5": 5 * 60,
    "M15": 15 * 60,
    "H1": 60 * 60,
    "H4": 4 * 60 * 60,
    "D1": 24 * 60 * 60,
}


# ==========================================================
# DATOS
# ==========================================================

def load_market_data(symbol, timeframe):

    file_path = (
        MARKET_DIR
        / symbol
        / f"{timeframe}.csv"
    )

    if not file_path.exists():

        raise FileNotFoundError(
            f"No existe {file_path}"
        )

    df = pd.read_csv(
        file_path,
        parse_dates=["time"],
    )

    df["time"] = pd.to_datetime(
        df["time"],
        utc=True,
    )

    df = (
        df
        .sort_values("time")
        .drop_duplicates(
            subset=["time"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    return df


# ==========================================================
# HUECOS
# ==========================================================

def crosses_weekend(previous_time, current_time):

    current_day = previous_time.normalize()

    end_day = current_time.normalize()

    while current_day <= end_day:

        if current_day.weekday() >= 5:
            return True

        current_day += pd.Timedelta(
            days=1
        )

    return False


def detect_recent_gaps(
    df,
    timeframe,
    lookback=50,
):

    recent = (
        df
        .tail(lookback)
        .copy()
        .reset_index(drop=True)
    )

    if len(recent) < 2:
        return []

    expected_delta = pd.Timedelta(
        seconds=TIMEFRAME_SECONDS[timeframe]
    )

    differences = recent["time"].diff()

    gap_indexes = differences[
        differences > expected_delta
    ].index

    gaps = []

    for index in gap_indexes:

        previous_time = recent.loc[
            index - 1,
            "time"
        ]

        current_time = recent.loc[
            index,
            "time"
        ]

        if crosses_weekend(
            previous_time,
            current_time,
        ):
            continue

        gaps.append(
            {
                "from": previous_time.isoformat(),
                "to": current_time.isoformat(),
                "seconds": int(
                    (
                        current_time
                        - previous_time
                    ).total_seconds()
                ),
            }
        )

    return gaps


# ==========================================================
# VELAS PARA EL LLM
# ==========================================================

def get_recent_candles(
    df,
    number_of_candles,
):

    candle_columns = [
        "time",
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
        "spread",
    ]

    recent = (
        df[candle_columns]
        .tail(number_of_candles)
    )

    result = []

    for _, row in recent.iterrows():

        result.append(
            {
                "time": row[
                    "time"
                ].isoformat(),

                "open": safe_number(
                    row["open"]
                ),

                "high": safe_number(
                    row["high"]
                ),

                "low": safe_number(
                    row["low"]
                ),

                "close": safe_number(
                    row["close"]
                ),

                "tick_volume": safe_number(
                    row["tick_volume"]
                ),

                "spread": safe_number(
                    row["spread"]
                ),
            }
        )

    return result


# ==========================================================
# TIMEFRAME STATE
# ==========================================================

def build_timeframe_state(
    symbol,
    timeframe,
):

    raw_df = load_market_data(
        symbol,
        timeframe,
    )

    df = add_indicators(
        raw_df
    )

    gaps = detect_recent_gaps(
        raw_df,
        timeframe,
    )

    last_row = raw_df.iloc[-1]

    duration = pd.Timedelta(
        seconds=TIMEFRAME_SECONDS[timeframe]
    )

    last_bar_open = (
        last_row["time"]
    )

    last_bar_close = (
        last_bar_open
        + duration
    )

    indicators = (
        get_indicator_snapshot(df)
    )

    quality = (
        "OK"
        if len(gaps) == 0
        else "WARN"
    )

    return {
        "bars_loaded": len(raw_df),

        "last_bar_open":
            last_bar_open.isoformat(),

        "last_bar_close":
            last_bar_close.isoformat(),

        "data_quality": {
            "status": quality,
            "unexpected_recent_gaps": gaps,
        },

        "indicators": indicators,

        "recent_candles":
            get_recent_candles(
                raw_df,
                RECENT_CANDLES[
                    timeframe
                ],
            ),
    }


# ==========================================================
# SYMBOL STATE
# ==========================================================

def build_symbol_state(symbol):

    state = {
        "symbol": symbol,
        "analysis_timeframe":
            ANALYSIS_TIMEFRAME,
        "timeframes": {},
    }

    for timeframe in TIMEFRAMES:

        state["timeframes"][
            timeframe
        ] = build_timeframe_state(
            symbol,
            timeframe,
        )

    analysis_tf = (
        state["timeframes"][
            ANALYSIS_TIMEFRAME
        ]
    )

    state["analysis_time"] = (
        analysis_tf[
            "last_bar_close"
        ]
    )

    warnings = []

    for timeframe, tf_state in (
        state["timeframes"].items()
    ):

        if (
            tf_state[
                "data_quality"
            ]["status"]
            != "OK"
        ):

            warnings.append(
                timeframe
            )

    state["data_quality"] = {
        "status":
            "OK"
            if not warnings
            else "WARN",

        "timeframes_with_warnings":
            warnings,
    }

    return state


# ==========================================================
# GUARDAR
# ==========================================================

def save_state(state):

    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    file_path = (
        STATE_DIR
        / f"{state['symbol']}.json"
    )

    with open(
        file_path,
        "w",
        encoding="utf-8",
    ) as file:

        json.dump(
            state,
            file,
            indent=2,
            ensure_ascii=False,
        )

    return file_path


# ==========================================================
# RESUMEN
# ==========================================================

def print_summary(state):

    symbol = state["symbol"]

    print()
    print("=" * 75)
    print(symbol)
    print("=" * 75)

    print(
        "Analysis time:",
        state["analysis_time"],
    )

    print(
        "Data quality:",
        state[
            "data_quality"
        ]["status"],
    )

    for timeframe in TIMEFRAMES:

        tf = (
            state[
                "timeframes"
            ][timeframe]
        )

        ind = tf["indicators"]

        print()
        print(
            f"{timeframe:<3}",
            "| close:",
            tf[
                "recent_candles"
            ][-1]["close"],
            "| RSI:",
            (
                round(
                    ind["rsi14"],
                    2,
                )
                if ind["rsi14"]
                is not None
                else None
            ),
            "| ATR:",
            (
                round(
                    ind["atr14"],
                    6,
                )
                if ind["atr14"]
                is not None
                else None
            ),
            "| EMA20:",
            (
                round(
                    ind["ema20"],
                    6,
                )
                if ind["ema20"]
                is not None
                else None
            ),
            "| EMA50:",
            (
                round(
                    ind["ema50"],
                    6,
                )
                if ind["ema50"]
                is not None
                else None
            ),
            "| EMA200:",
            (
                round(
                    ind["ema200"],
                    6,
                )
                if ind["ema200"]
                is not None
                else None
            ),
        )


# ==========================================================
# MAIN
# ==========================================================

def main():

    print("=" * 75)
    print(
        "TRADING AGENT - "
        "MARKET STATE MULTI-TIMEFRAME"
    )
    print("=" * 75)

    for symbol in SYMBOLS:

        state = build_symbol_state(
            symbol
        )

        file_path = save_state(
            state
        )

        print_summary(
            state
        )

        print(
            "\nGuardado:",
            file_path,
        )


if __name__ == "__main__":
    main()