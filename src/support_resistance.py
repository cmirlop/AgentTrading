import pandas as pd


TIMEFRAMES = (
    "M15",
    "H1",
    "H4",
)

LOOKBACK_BARS = {
    "M15": 160,
    "H1": 120,
    "H4": 90,
}

PIVOT_RADIUS = {
    "M15": 2,
    "H1": 2,
    "H4": 2,
}

TIMEFRAME_SCORE = {
    "M15": 8,
    "H1": 14,
    "H4": 20,
}

POINT_BY_SYMBOL = {
    "EURUSD": 0.00001,
    "USDCAD": 0.00001,
    "AUDUSD": 0.00001,
}

MERGE_ATR_FACTOR = 0.35
ZONE_HALF_WIDTH_ATR = 0.15

MAX_LEVELS_PER_SIDE = 4


def _utc(value):

    value = pd.Timestamp(
        value
    )

    if value.tzinfo is None:

        value = value.tz_localize(
            "UTC"
        )

    else:

        value = value.tz_convert(
            "UTC"
        )

    return value


def _closed_history(
    dataframe,
    analysis_time,
    bars,
):

    analysis_time = _utc(
        analysis_time
    )

    end_index = (
        dataframe[
            "bar_close"
        ].searchsorted(
            analysis_time,
            side="right",
        )
    )

    if end_index <= 0:
        return dataframe.iloc[0:0]

    start_index = max(
        0,
        end_index - bars,
    )

    return dataframe.iloc[
        start_index:end_index
    ].copy()


def _find_pivots(
    dataframe,
    timeframe,
):

    radius = PIVOT_RADIUS[
        timeframe
    ]

    pivots = []

    if len(dataframe) < (
        radius * 2 + 1
    ):

        return pivots

    for index in range(
        radius,
        len(dataframe) - radius,
    ):

        row = dataframe.iloc[
            index
        ]

        left = dataframe.iloc[
            index - radius:index
        ]

        right = dataframe.iloc[
            index + 1:index + radius + 1
        ]

        high = float(
            row["high"]
        )

        low = float(
            row["low"]
        )

        # ----------------------------------------------
        # PIVOT HIGH
        # ----------------------------------------------

        if (
            high
            >= float(
                left["high"].max()
            )
            and
            high
            > float(
                right["high"].max()
            )
        ):

            pivots.append(
                {
                    "price":
                        high,

                    "pivot_type":
                        "HIGH",

                    "timeframe":
                        timeframe,

                    "time":
                        row[
                            "bar_close"
                        ],
                }
            )

        # ----------------------------------------------
        # PIVOT LOW
        # ----------------------------------------------

        if (
            low
            <= float(
                left["low"].min()
            )
            and
            low
            < float(
                right["low"].min()
            )
        ):

            pivots.append(
                {
                    "price":
                        low,

                    "pivot_type":
                        "LOW",

                    "timeframe":
                        timeframe,

                    "time":
                        row[
                            "bar_close"
                        ],
                }
            )

    return pivots


def _cluster_pivots(
    pivots,
    merge_distance,
):

    clusters = []

    for pivot in sorted(
        pivots,
        key=lambda item:
            item["price"],
    ):

        best_cluster = None
        best_distance = None

        for cluster in clusters:

            distance = abs(
                pivot["price"]
                -
                cluster["center"]
            )

            if (
                distance
                <= merge_distance
            ):

                if (
                    best_distance is None
                    or
                    distance < best_distance
                ):

                    best_cluster = cluster
                    best_distance = distance

        if best_cluster is None:

            clusters.append(
                {
                    "center":
                        pivot["price"],

                    "points":
                        [pivot],
                }
            )

            continue

        best_cluster[
            "points"
        ].append(
            pivot
        )

        weights = []

        prices = []

        for item in (
            best_cluster[
                "points"
            ]
        ):

            weight = (
                TIMEFRAME_SCORE[
                    item[
                        "timeframe"
                    ]
                ]
            )

            weights.append(
                weight
            )

            prices.append(
                item[
                    "price"
                ]
                * weight
            )

        best_cluster[
            "center"
        ] = (
            sum(prices)
            /
            sum(weights)
        )

    return clusters


def _strength(
    points,
    analysis_time,
):

    timeframes = set(
        item[
            "timeframe"
        ]
        for item in points
    )

    touches = len(
        points
    )

    touch_score = min(
        40,
        touches * 8,
    )

    timeframe_score = min(
        35,
        sum(
            TIMEFRAME_SCORE[
                timeframe
            ]
            for timeframe
            in timeframes
        ),
    )

    last_touch = max(
        _utc(
            item["time"]
        )
        for item in points
    )

    age_hours = (
        _utc(
            analysis_time
        )
        - last_touch
    ).total_seconds() / 3600.0

    if age_hours <= 6:

        recency_score = 20

    elif age_hours <= 24:

        recency_score = 15

    elif age_hours <= 72:

        recency_score = 10

    elif age_hours <= 168:

        recency_score = 5

    else:

        recency_score = 0

    return min(
        100,
        touch_score
        + timeframe_score
        + recency_score,
    )


def detect_support_resistance(
    symbol,
    analysis_time,
    prepared_history,
):

    analysis_time = _utc(
        analysis_time
    )

    point = POINT_BY_SYMBOL[
        symbol
    ]

    # ======================================================
    # PRECIO / ATR DE REFERENCIA
    # ======================================================

    m15 = _closed_history(
        prepared_history[
            "M15"
        ],
        analysis_time,
        LOOKBACK_BARS[
            "M15"
        ],
    )

    if m15.empty:

        return {
            "status":
                "NO_DATA",

            "symbol":
                symbol,
        }

    latest = m15.iloc[-1]

    current_price = float(
        latest[
            "close"
        ]
    )

    reference_atr = float(
        latest[
            "atr14"
        ]
    )

    if (
        pd.isna(
            reference_atr
        )
        or
        reference_atr <= 0
    ):

        return {
            "status":
                "ATR_UNAVAILABLE",

            "symbol":
                symbol,
        }

    merge_distance = max(
        reference_atr
        * MERGE_ATR_FACTOR,

        point * 5,
    )

    half_width = max(
        reference_atr
        * ZONE_HALF_WIDTH_ATR,

        point * 3,
    )

    # ======================================================
    # PIVOTS M15 / H1 / H4
    # ======================================================

    pivots = []

    bars_used = {}

    for timeframe in TIMEFRAMES:

        dataframe = (
            _closed_history(
                prepared_history[
                    timeframe
                ],
                analysis_time,
                LOOKBACK_BARS[
                    timeframe
                ],
            )
        )

        bars_used[
            timeframe
        ] = len(
            dataframe
        )

        pivots.extend(
            _find_pivots(
                dataframe,
                timeframe,
            )
        )

    clusters = _cluster_pivots(
        pivots,
        merge_distance,
    )

    zones = []

    # ======================================================
    # CONVERTIR CLUSTERS EN ZONAS
    # ======================================================

    for cluster in clusters:

        points = cluster[
            "points"
        ]

        timeframes = sorted(
            {
                item[
                    "timeframe"
                ]
                for item in points
            },
            key=lambda tf:
                TIMEFRAMES.index(
                    tf
                ),
        )

        # Evitamos zonas débiles de un único pivot pequeño.
        if (
            len(points) < 2
            and
            "H4"
            not in timeframes
        ):

            continue

        prices = [
            item[
                "price"
            ]
            for item in points
        ]

        zone_low = (
            min(prices)
            - half_width
        )

        zone_high = (
            max(prices)
            + half_width
        )

        midpoint = (
            zone_low
            + zone_high
        ) / 2.0

        if zone_high < current_price:

            role = "SUPPORT"

            distance = (
                current_price
                - zone_high
            )

        elif zone_low > current_price:

            role = "RESISTANCE"

            distance = (
                zone_low
                - current_price
            )

        else:

            role = "ACTIVE_ZONE"
            distance = 0.0

        last_touch = max(
            _utc(
                item[
                    "time"
                ]
            )
            for item in points
        )

        zones.append(
            {
                "role":
                    role,

                "low":
                    round(
                        zone_low,
                        5,
                    ),

                "high":
                    round(
                        zone_high,
                        5,
                    ),

                "mid":
                    round(
                        midpoint,
                        5,
                    ),

                "touches":
                    len(
                        points
                    ),

                "timeframes":
                    timeframes,

                "pivot_types":
                    sorted(
                        {
                            item[
                                "pivot_type"
                            ]
                            for item
                            in points
                        }
                    ),

                "last_touch":
                    last_touch.isoformat(),

                "strength":
                    _strength(
                        points,
                        analysis_time,
                    ),

                "distance":
                    round(
                        distance,
                        5,
                    ),

                "distance_atr":
                    round(
                        distance
                        /
                        reference_atr,
                        2,
                    ),
            }
        )

    # ======================================================
    # ORDENAR POR CERCANIA
    # ======================================================

    supports = sorted(
        [
            zone
            for zone in zones
            if zone[
                "role"
            ]
            == "SUPPORT"
        ],
        key=lambda zone:
            zone[
                "distance"
            ],
    )

    resistances = sorted(
        [
            zone
            for zone in zones
            if zone[
                "role"
            ]
            == "RESISTANCE"
        ],
        key=lambda zone:
            zone[
                "distance"
            ],
    )

    active_zones = [
        zone
        for zone in zones
        if zone[
            "role"
        ]
        == "ACTIVE_ZONE"
    ]

    return {
        "status":
            "OK",

        "symbol":
            symbol,

        "analysis_time":
            analysis_time.isoformat(),

        "current_price":
            round(
                current_price,
                5,
            ),

        "reference_atr_m15":
            round(
                reference_atr,
                6,
            ),

        "bars_used":
            bars_used,

        "nearest_support":
            (
                supports[0]
                if supports
                else None
            ),

        "nearest_resistance":
            (
                resistances[0]
                if resistances
                else None
            ),

        "active_zones":
            active_zones,

        "supports":
            supports[
                :MAX_LEVELS_PER_SIDE
            ],

        "resistances":
            resistances[
                :MAX_LEVELS_PER_SIDE
            ],
    }