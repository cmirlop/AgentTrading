def unique_list(
    values,
    max_items=None,
):

    result = list(
        dict.fromkeys(
            values or []
        )
    )

    if max_items is not None:
        result = result[
            :max_items
        ]

    return result