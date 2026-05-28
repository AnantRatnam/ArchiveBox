# ruff: noqa
def follow(file, sleep_sec=0.1) -> Iterator[str]:
    """Yield each line from a file as they are written.
    `sleep_sec` is the time to sleep after empty reads."""
    line = ""
    while True:
        tmp = file.readline()
        if tmp is not None and tmp != "":
            line += tmp
            if line.endswith("\n"):
                yield line
                line = ""
        elif sleep_sec:
            time.sleep(sleep_sec)


def tail_worker_logs(log_path: str):
    get_or_create_supervisord_process(daemonize=False)

    from rich.live import Live
    from rich.table import Table

    table = Table()
    table.add_column("TS")
    table.add_column("URL")

    try:
        with Live(table, refresh_per_second=1) as live:  # update 4 times a second to feel fluid
            with open(log_path) as f:
                for line in follow(f):
                    if "://" in line:
                        live.console.print(f"Working on: {line.strip()}")
                    # table.add_row("123124234", line.strip())
    except (KeyboardInterrupt, BrokenPipeError, OSError):
        STDERR.print("\n[🛑] Got Ctrl+C, stopping gracefully...")
    except SystemExit:
        pass


def watch_worker(supervisor, daemon_name, interval=5):
    """loop continuously and monitor worker's health"""
    while True:
        proc = get_worker(supervisor, daemon_name)
        if not proc:
            raise Exception("Worker disappeared while running! " + daemon_name)

        if proc["statename"] == "STOPPED":
            return proc

        if proc["statename"] == "RUNNING":
            time.sleep(1)
            continue

        if proc["statename"] in ("STARTING", "BACKOFF", "FATAL", "EXITED", "STOPPING"):
            print(f"[🦸‍♂️] WARNING: Worker {daemon_name} {proc['statename']} {proc['description']}")
            time.sleep(interval)
            continue


def start_cli_workers(watch=False):
    from archivebox.config.common import get_config

    supervisor = get_or_create_supervisord_process(daemonize=False)

    sonic_worker = get_sonic_supervisord_worker_from_plugin(get_config())
    workers = [(RUNNER_WORKER, False)]
    if sonic_worker is not None:
        workers.insert(0, (sonic_worker, False))

    sync_supervisord_workers(supervisor, workers, prune=True)

    if watch:
        try:
            # Block on supervisord process - it will handle signals and stop children
            if _supervisord_proc:
                _supervisord_proc.wait()
            else:
                # Fallback to watching worker if no proc reference
                watch_worker(supervisor, RUNNER_WORKER["name"])
        except (KeyboardInterrupt, BrokenPipeError, OSError):
            STDERR.print("\n[🛑] Got Ctrl+C, stopping gracefully...")
        except SystemExit:
            pass
        except BaseException as e:
            STDERR.print(f"\n[🛑] Got {e.__class__.__name__} exception, stopping gracefully...")
        finally:
            # Ensure supervisord and all children are stopped
            stop_existing_supervisord_process()
            time.sleep(1.0)  # Give processes time to fully terminate
    return [RUNNER_WORKER]
