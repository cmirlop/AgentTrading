import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = (
    Path(__file__)
    .resolve()
    .parent
)

LOG_DIR = (
    BASE_DIR
    / "logs"
    / "cycles"
)


STAGES = [
    (
        "SYNC_MARKET_DATA",
        "sync_market_data.py",
    ),
    (
        "BUILD_MARKET_STATE",
        "build_market_state.py",
    ),
    (
        "RUN_MARKET_AGENTS",
        "run_market_agents.py",
    ),
    (
        "BUILD_RUNTIME_STATE",
        "build_runtime_state.py",
    ),
    (
        "BUILD_CALENDAR_STATE",
        "build_calendar_state.py",
    ),
    (
        "FULL_DRY_PIPELINE",
        "run_dry_pipeline.py",
    ),
]


def utc_now():

    return datetime.now(
        timezone.utc
    )


def write_log(
    file,
    text,
):

    print(
        text,
        flush=True,
    )

    file.write(
        text + "\n"
    )

    file.flush()


def run_stage(
    name,
    script,
    log_file,
):

    write_log(
        log_file,
        "",
    )

    write_log(
        log_file,
        "=" * 100,
    )

    write_log(
        log_file,
        f"STAGE: {name}",
    )

    write_log(
        log_file,
        f"SCRIPT: {script}",
    )

    write_log(
        log_file,
        "=" * 100,
    )

    start = time.perf_counter()

    process = subprocess.Popen(
        [
            sys.executable,
            str(
                BASE_DIR
                / script
            ),
        ],

        cwd=BASE_DIR,

        stdout=subprocess.PIPE,

        stderr=subprocess.STDOUT,

        text=True,

        encoding="utf-8",

        errors="replace",

        bufsize=1,
    )

    assert (
        process.stdout
        is not None
    )

    for line in process.stdout:

        line = line.rstrip()

        write_log(
            log_file,
            line,
        )

    return_code = (
        process.wait()
    )

    elapsed = (
        time.perf_counter()
        - start
    )

    write_log(
        log_file,
        "",
    )

    write_log(
        log_file,
        (
            f"[STAGE RESULT] "
            f"{name} | "
            f"return_code={return_code} | "
            f"elapsed={elapsed:.2f}s"
        ),
    )

    if return_code != 0:

        raise RuntimeError(
            f"Stage {name} fallo "
            f"con codigo {return_code}"
        )

    return elapsed


def main():

    cycle_start_utc = (
        utc_now()
    )

    cycle_start_perf = (
        time.perf_counter()
    )

    LOG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    log_path = (
        LOG_DIR
        / (
            cycle_start_utc
            .strftime(
                "%Y-%m-%d"
            )
            + ".log"
        )
    )

    with open(
        log_path,
        "a",
        encoding="utf-8",
    ) as log_file:

        write_log(
            log_file,
            "",
        )

        write_log(
            log_file,
            "#" * 100,
        )

        write_log(
            log_file,
            "TRADING AGENT - FULL CYCLE",
        )

        write_log(
            log_file,
            (
                "Cycle UTC: "
                + cycle_start_utc
                .isoformat()
            ),
        )

        write_log(
            log_file,
            "#" * 100,
        )

        stage_times = {}

        try:

            for (
                stage_name,
                script,
            ) in STAGES:

                elapsed = (
                    run_stage(
                        stage_name,
                        script,
                        log_file,
                    )
                )

                stage_times[
                    stage_name
                ] = round(
                    elapsed,
                    2,
                )

        except Exception as exc:

            total_elapsed = (
                time.perf_counter()
                - cycle_start_perf
            )

            write_log(
                log_file,
                "",
            )

            write_log(
                log_file,
                "!" * 100,
            )

            write_log(
                log_file,
                (
                    "[CYCLE FAILED] "
                    + str(exc)
                ),
            )

            write_log(
                log_file,
                (
                    "Elapsed: "
                    f"{total_elapsed:.2f}s"
                ),
            )

            write_log(
                log_file,
                "!" * 100,
            )

            raise

        total_elapsed = (
            time.perf_counter()
            - cycle_start_perf
        )

        write_log(
            log_file,
            "",
        )

        write_log(
            log_file,
            "#" * 100,
        )

        write_log(
            log_file,
            "[CYCLE COMPLETED]",
        )

        write_log(
            log_file,
            (
                "Total elapsed: "
                f"{total_elapsed:.2f}s"
            ),
        )

        write_log(
            log_file,
            (
                "Stages: "
                + str(
                    stage_times
                )
            ),
        )

        write_log(
            log_file,
            (
                "Log: "
                + str(
                    log_path
                )
            ),
        )

        write_log(
            log_file,
            "#" * 100,
        )


if __name__ == "__main__":
    main()