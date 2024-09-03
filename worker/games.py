import copy
import ctypes
import hashlib
import io
import json
import math
import multiprocessing
import os
import platform
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from base64 import b64decode
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Empty, Queue
from zipfile import ZipFile

import requests

BASELINE_NPS = 198243
IS_WINDOWS = "windows" in platform.system().lower()
IS_MACOS = "darwin" in platform.system().lower()
LOGFILE = "api.log"

LOG_LOCK = threading.Lock()


class WorkerException(Exception):
    def __new__(cls, msg, e=None):
        if e is not None and isinstance(e, WorkerException):
            # Note that this forwards also instances of
            # subclasses of WorkerException, e.g.
            # FatalException.
            return e
        else:
            return super().__new__(cls, msg)

    def __init__(self, *args, **kw):
        pass


class FatalException(WorkerException):
    pass


class RunException(WorkerException):
    pass


def is_windows_64bit():
    if "PROCESSOR_ARCHITEW6432" in os.environ:
        return True
    return os.environ["PROCESSOR_ARCHITECTURE"].endswith("64")


def is_64bit():
    if IS_WINDOWS:
        return is_windows_64bit()
    return "64" in platform.architecture()[0]


HTTP_TIMEOUT = 30.0
FASTCHESS_KILL_TIMEOUT = 15.0
UPDATE_RETRY_TIME = 15.0

RAWCONTENT_HOST = "https://raw.githubusercontent.com"
API_HOST = "https://api.github.com"
EXE_SUFFIX = ".exe" if IS_WINDOWS else ""


def log(s):
    logfile = Path(__file__).resolve().parent / LOGFILE
    with LOG_LOCK:
        with open(logfile, "a") as f:
            f.write("{} : {}\n".format(datetime.now(timezone.utc), s))


def backup_log():
    try:
        logfile = Path(__file__).resolve().parent / LOGFILE
        logfile_previous = logfile.with_suffix(logfile.suffix + ".previous")
        if logfile.exists():
            print("Moving logfile {} to {}".format(logfile, logfile_previous))
            with LOG_LOCK:
                logfile.replace(logfile_previous)
    except Exception as e:
        print(
            "Exception moving log:\n",
            e,
            sep="",
            file=sys.stderr,
        )


def str_signal(signal_):
    try:
        return signal.Signals(signal_).name
    except (ValueError, AttributeError):
        return "SIG<{}>".format(signal_)


def format_return_code(r):
    if r < 0:
        return str_signal(-r)
    elif r >= 256:
        return str(hex(r))
    else:
        return str(r)


def send_ctrl_c(pid):
    kernel = ctypes.windll.kernel32
    _ = (
        kernel.FreeConsole()
        and kernel.SetConsoleCtrlHandler(None, True)
        and kernel.AttachConsole(pid)
        and kernel.GenerateConsoleCtrlEvent(0, 0)
    )


def send_sigint(p):
    if IS_WINDOWS:
        if p.poll() is None:
            proc = multiprocessing.Process(target=send_ctrl_c, args=(p.pid,))
            proc.start()
            proc.join()
    else:
        p.send_signal(signal.SIGINT)


def cache_read(cache, name):
    """Read a binary blob of data from a global cache on disk, None if not available"""
    if cache == "":
        return None

    try:
        return (Path(cache) / name).read_bytes()
    except Exception as e:
        return None


def cache_write(cache, name, data):
    """Write a binary blob of data to a global cache on disk in an atomic way, skip if not available"""
    if cache == "":
        return

    try:
        temp_file = tempfile.NamedTemporaryFile(dir=cache, delete=False)
        temp_file.write(data)
        temp_file.flush()
        os.fsync(temp_file.fileno())  # Ensure data is written to disk
        temp_file.close()

        # try linking, which is atomic, and will fail if the file exists
        try:
            os.link(temp_file.name, Path(cache) / name)
        except OSError:
            pass

        # Remove the temporary file
        os.remove(temp_file.name)
    except Exception as e:
        return


# See https://stackoverflow.com/questions/16511337/correct-way-to-try-except-using-python-requests-module
# for background.
# It may be useful to introduce more refined http exception handling in the future.


def requests_get(remote, *args, **kw):
    # A lightweight wrapper around requests.get()
    try:
        result = requests.get(remote, *args, **kw)
        result.raise_for_status()  # also catch return codes >= 400
    except Exception as e:
        print(
            "Exception in requests.get():\n",
            e,
            sep="",
            file=sys.stderr,
        )
        raise WorkerException("Get request to {} failed".format(remote), e=e)

    return result


def requests_post(remote, *args, **kw):
    # A lightweight wrapper around requests.post()
    try:
        result = requests.post(remote, *args, **kw)
    except Exception as e:
        print(
            "Exception in requests.post():\n",
            e,
            sep="",
            file=sys.stderr,
        )
        raise WorkerException("Post request to {} failed".format(remote), e=e)

    return result


def send_api_post_request(api_url, payload, quiet=False):
    t0 = datetime.now(timezone.utc)
    response = requests_post(
        api_url,
        data=json.dumps(payload),
        headers={"Content-Type": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    valid_response = True
    try:
        response = response.json()
    except:
        valid_response = False
    if valid_response and not isinstance(response, dict):
        valid_response = False
    if not valid_response:
        message = (
            "The reply to post request {} was not a json encoded dictionary".format(
                api_url
            )
        )
        print(
            "Exception in send_api_post_request():\n",
            message,
            sep="",
            file=sys.stderr,
        )
        raise WorkerException(message)
    if "error" in response:
        print("Error from remote: {}".format(response["error"]))

    t1 = datetime.now(timezone.utc)
    w = 1000 * (t1 - t0).total_seconds()
    s = 1000 * response["duration"]
    log(
        "{:6.2f} ms (s)  {:7.2f} ms (w)  {}".format(
            s,
            w,
            api_url,
        )
    )
    if not quiet:
        if "info" in response:
            print("Info from remote: {}".format(response["info"]))
        print(
            "Post request {} handled in {:.2f}ms (server: {:.2f}ms)".format(
                api_url, w, s
            )
        )
    return response


def github_api(repo):
    """Convert from https://github.com/<user>/<repo>
    To https://api.github.com/repos/<user>/<repo>"""
    return repo.replace("https://github.com", "https://api.github.com/repos")


def required_nets(engine):
    nets = {}
    pattern = re.compile(r"(EvalFile\w*)\s+.*\s+(nn-[a-f0-9]{12}.network)")
    print("Obtaining EvalFile of {} ...".format(os.path.basename(engine)))
    try:
        with subprocess.Popen(
            [engine, "uci"],
            stdout=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
            close_fds=not IS_WINDOWS,
        ) as p:
            for line in iter(p.stdout.readline, ""):
                match = pattern.search(line)
                if match:
                    nets[match.group(1)] = match.group(2)

    except (OSError, subprocess.SubprocessError) as e:
        raise WorkerException(
            "Unable to obtain name for required net. Error: {}".format(str(e))
        )

    if p.returncode != 0:
        raise WorkerException(
            "UCI exited with non-zero code {}".format(format_return_code(p.returncode))
        )

    return nets


def required_value_from_source():
    pattern = re.compile("nn-[a-f0-9]{12}.network")

    with open("src/networks/value.rs", "r") as srcfile:
        for line in srcfile:
            if "ValueFileDefaultName" in line:
                m = pattern.search(line)
                if m:
                    return m.group(0)


def required_policy_from_source():
    pattern = re.compile("nn-[a-f0-9]{12}.network")

    with open("src/networks/policy.rs", "r") as srcfile:
        for line in srcfile:
            if "PolicyFileDefaultName" in line:
                m = pattern.search(line)
                if m:
                    return m.group(0)


def download_net(remote, testing_dir, net, global_cache):
    content = cache_read(global_cache, net)

    if content is None:
        url = remote + "/api/nn/" + net
        print("Downloading {}".format(net))
        content = requests_get(url, allow_redirects=True, timeout=HTTP_TIMEOUT).content
        hash = hashlib.sha256(content).hexdigest()
        if hash[:12] == net[3:15]:
            cache_write(global_cache, net, content)
    else:
        print("Using {} from global cache".format(net))

    (testing_dir / net).write_bytes(content)


def validate_net(testing_dir, net):
    hash = hashlib.sha256((testing_dir / net).read_bytes()).hexdigest()
    return hash[:12] == net[3:15]


def establish_validated_net(remote, testing_dir, net, global_cache):
    if not (testing_dir / net).exists() or not validate_net(testing_dir, net):
        attempt = 0
        while True:
            try:
                attempt += 1
                download_net(remote, testing_dir, net, global_cache)
                if not validate_net(testing_dir, net):
                    raise WorkerException(
                        "Failed to validate the network: {}".format(net)
                    )
                break
            except FatalException:
                raise
            except WorkerException:
                if attempt > 5:
                    raise
                waitTime = UPDATE_RETRY_TIME * attempt
                print(
                    "Failed to download {} in attempt {}, trying in {} seconds.".format(
                        net, attempt, waitTime
                    )
                )
                time.sleep(waitTime)


def run_single_bench(engine, queue):
    bench_sig = None
    bench_nps = None

    try:
        p = subprocess.Popen(
            [engine, "bench"],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
            close_fds=not IS_WINDOWS,
        )

        for line in iter(p.stdout.readline, ""):
            if "Bench: " in line:
                spl = line.split(" ")
                bench_sig = int(spl[1].strip())
                bench_nps = float(spl[3].strip())

        queue.put((bench_sig, bench_nps))
    except Exception as e:
        raise RunException(
            "Unable to parse bench output of {}. Error occurred while processing line: '{}'. Error: {}".format(
                os.path.basename(engine), line, e
            )
        )


def verify_signature(engine, signature, active_cores):
    queue = multiprocessing.Queue()

    processes = [
        multiprocessing.Process(
            target=run_single_bench,
            args=(engine, queue),
        )
        for _ in range(active_cores)
    ]

    for p in processes:
        p.start()

    results = [queue.get() for _ in range(active_cores)]
    bench_nps = 0.0

    for sig, nps in results:

        bench_nps += nps

        if int(sig) != int(signature):
            message = "Wrong bench in {}, user expected: {} but worker got: {}".format(
                os.path.basename(engine),
                signature,
                sig,
            )
            raise RunException(message)

    bench_nps /= active_cores

    return bench_nps


def download_from_github_raw(
    item, owner="official-monty", repo="books", branch="master"
):
    item_url = "{}/{}/{}/{}/{}".format(RAWCONTENT_HOST, owner, repo, branch, item)
    print("Downloading {}".format(item_url))
    return requests_get(item_url, timeout=HTTP_TIMEOUT).content


def download_from_github_api(
    item, owner="official-monty", repo="books", branch="master"
):
    item_url = "{}/repos/{}/{}/contents/{}?ref={}".format(
        API_HOST, owner, repo, item, branch
    )
    print("Downloading {}".format(item_url))
    git_url = requests_get(item_url, timeout=HTTP_TIMEOUT).json()["git_url"]
    return b64decode(requests_get(git_url, timeout=HTTP_TIMEOUT).json()["content"])


def download_from_github(item, owner="official-monty", repo="books", branch="master"):
    try:
        blob = download_from_github_raw(item, owner=owner, repo=repo, branch=branch)
    except FatalException:
        raise
    except Exception as e:
        print(f"Downloading {item} failed: {str(e)}. Trying the GitHub api.")
        try:
            blob = download_from_github_api(item, owner=owner, repo=repo, branch=branch)
        except Exception as e:
            raise WorkerException(f"Unable to download {item}", e=e)
    return blob


def unzip(blob, save_dir):
    cd = os.getcwd()
    os.chdir(save_dir)
    zipball = io.BytesIO(blob)
    with ZipFile(zipball) as zip_file:
        zip_file.extractall()
        file_list = zip_file.infolist()
    os.chdir(cd)
    return file_list


def setup_engine(
    destination,
    worker_dir,
    testing_dir,
    remote,
    sha,
    repo_url,
    global_cache,
    datagen=False,
):
    """Download and build sources in a temporary directory then move exe to destination"""
    tmp_dir = Path(tempfile.mkdtemp(dir=worker_dir))

    try:
        blob = cache_read(global_cache, sha + ".zip")

        if blob is None:
            item_url = github_api(repo_url) + "/zipball/" + sha
            print("Downloading {}".format(item_url))
            blob = requests_get(item_url).content
            blob_needs_write = True
        else:
            blob_needs_write = False
            print("Using {} from global cache".format(sha + ".zip"))

        file_list = unzip(blob, tmp_dir)
        # once unzipped without error we can write as needed
        if blob_needs_write:
            cache_write(global_cache, sha + ".zip", blob)

        prefix = os.path.commonprefix([n.filename for n in file_list])
        os.chdir(tmp_dir / prefix)

        evalfile = required_value_from_source()
        print("Build uses default value net:", evalfile)
        establish_validated_net(remote, testing_dir, evalfile, global_cache)
        shutil.copyfile(testing_dir / evalfile, evalfile)

        policyfile = required_policy_from_source()
        print("Build uses default policy net:", policyfile)
        establish_validated_net(remote, testing_dir, policyfile, global_cache)
        shutil.copyfile(testing_dir / policyfile, policyfile)

        cmd = ["make", "gen" if datagen else "montytest", f"EXE={destination}"]

        if os.path.exists(destination):
            raise FatalException("Another worker is running in the same directory!")

        with subprocess.Popen(
            cmd,
            start_new_session=False if IS_WINDOWS else True,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
            close_fds=not IS_WINDOWS,
        ) as p:
            try:
                errors = p.stderr.readlines()
            except Exception as e:
                if not IS_WINDOWS:
                    os.killpg(p.pid, signal.SIGINT)
                raise WorkerException(
                    f"Executing {cmd} raised Exception: {type(e).__name__}: {e}",
                    e=e,
                )
        if p.returncode:
            raise WorkerException("Executing {} failed. Error: {}".format(cmd, errors))
    finally:
        os.chdir(worker_dir)
        shutil.rmtree(tmp_dir)


def kill_process(p):
    p_name = os.path.basename(p.args[0])
    print("Killing {} with pid {} ... ".format(p_name, p.pid), end="", flush=True)
    try:
        if IS_WINDOWS:
            # p.kill() doesn't kill subprocesses on Windows.
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT,
            )
        else:
            p.kill()
    except Exception as e:
        print(
            "\nException killing {} with pid {}, possibly already terminated:\n".format(
                p_name, p.pid
            ),
            e,
            sep="",
            file=sys.stderr,
        )
    else:
        print("killed", flush=True)


def adjust_tc(tc, factor):
    # Parse the time control in cutechess format.
    chunks = tc.split("+")
    increment = 0.0
    if len(chunks) == 2:
        increment = float(chunks[1])

    chunks = chunks[0].split("/")
    num_moves = 0
    if len(chunks) == 2:
        num_moves = int(chunks[0])

    time_tc = chunks[-1]
    chunks = time_tc.split(":")
    if len(chunks) == 2:
        time_tc = float(chunks[0]) * 60 + float(chunks[1])
    else:
        time_tc = float(chunks[0])

    # Rebuild scaled_tc now: cutechess-cli and monty parse 3 decimal places.
    scaled_tc = "{:.3f}".format(time_tc * factor)
    tc_limit = time_tc * factor * 3
    if increment > 0.0:
        scaled_tc += "+{:.3f}".format(increment * factor)
        tc_limit += increment * factor * 200
    if num_moves > 0:
        scaled_tc = "{}/{}".format(num_moves, scaled_tc)
        tc_limit *= 100.0 / num_moves

    print("CPU factor : {} - tc adjusted to {}".format(factor, scaled_tc))
    return scaled_tc, tc_limit


def enqueue_output(stream, queue):
    for line in iter(stream.readline, ""):
        queue.put(line)


def parse_fastchess_output(
    p, current_state, remote, result, spsa_tuning, games_to_play, batch_size, tc_limit
):
    hash_pattern = re.compile(r"(Base|New)-[a-f0-9]+")

    def shorten_hash(match):
        word = match.group(0).split("-")
        return "-".join([word[0], word[1][:10]])

    saved_stats = copy.deepcopy(result["stats"])

    # patterns used to obtain fastchess WLD and ptnml results from the following block of info:
    # --------------------------------------------------
    # Results of New-e443b2459e vs Base-e443b2459e (0.601+0.006, 1t, 16MB, UHO_Lichess_4852_v1.epd):
    # Elo: -9.20 +/- 20.93, nElo: -11.50 +/- 26.11
    # LOS: 19.41 %, DrawRatio: 42.35 %, PairsRatio: 0.88
    # Games: 680, Wins: 248, Losses: 266, Draws: 166, Points: 331.0 (48.68 %)
    # Ptnml(0-2): [43, 61, 144, 55, 37], WL/DD Ratio: 4.76
    # --------------------------------------------------
    pattern_WLD = re.compile(
        r"Games: ([0-9]+), Wins: ([0-9]+), Losses: ([0-9]+), Draws: ([0-9]+), Points: ([0-9.]+) \("
    )
    pattern_ptnml = re.compile(
        r"Ptnml\(0-2\): \[([0-9]+), ([0-9]+), ([0-9]+), ([0-9]+), ([0-9]+)\]"
    )
    fastchess_WLD_results = None
    fastchess_ptnml_results = None

    q = Queue()
    t_output = threading.Thread(target=enqueue_output, args=(p.stdout, q), daemon=True)
    t_output.start()
    t_error = threading.Thread(target=enqueue_output, args=(p.stderr, q), daemon=True)
    t_error.start()

    end_time = datetime.now(timezone.utc) + timedelta(seconds=tc_limit)
    print("TC limit {} End time: {}".format(tc_limit, end_time))

    num_games_updated = 0
    while datetime.now(timezone.utc) < end_time:
        try:
            line = q.get_nowait().strip()
        except Empty:
            if p.poll() is not None:
                break
            time.sleep(0.1)
            continue

        line = hash_pattern.sub(shorten_hash, line)
        print(line, flush=True)

        # Have we reached the end of the match? Then just exit.
        if "Finished match" in line:
            if num_games_updated == games_to_play:
                print("Finished match cleanly")
            else:
                raise WorkerException(
                    "Finished match uncleanly {} vs. required {}".format(
                        num_games_updated, games_to_play
                    )
                )

        # Parse line like this:
        # Warning: New-SHA doesn't have option ThreatBySafePawn
        if "Warning:" in line and "doesn't have option" in line:
            message = r'fastchess says: "{}"'.format(line)
            raise RunException(message)

        # Parse line like this:
        # Warning: Invalid value for option P: -354
        if "Warning:" in line and "Invalid value" in line:
            message = r'fastchess says: "{}"'.format(line)
            raise RunException(message)

        # Parse line like this:
        # Finished game 1 (monty vs base): 0-1 {White disconnects}
        if "disconnects" in line or "connection stalls" in line:
            result["stats"]["crashes"] += 1

        if "on time" in line:
            result["stats"]["time_losses"] += 1

        # fastchess WLD and pentanomial output parsing
        m = pattern_WLD.search(line)
        if m:
            try:
                fastchess_WLD_results = {
                    "games": int(m.group(1)),
                    "wins": int(m.group(2)),
                    "losses": int(m.group(3)),
                    "draws": int(m.group(4)),
                    "points": float(m.group(5)),
                }
            except Exception as e:
                raise WorkerException(
                    "Failed to parse WLD line: {} leading to: {}".format(line, str(e))
                )

        m = pattern_ptnml.search(line)
        if m:
            try:
                fastchess_ptnml_results = [int(m.group(i)) for i in range(1, 6)]
            except Exception as e:
                raise WorkerException(
                    "Failed to parse ptnml line: {} leading to: {}".format(line, str(e))
                )

        # if we have parsed the block properly let's update results
        if (fastchess_ptnml_results is not None) and (
            fastchess_WLD_results is not None
        ):
            result["stats"]["pentanomial"] = [
                fastchess_ptnml_results[i] + saved_stats["pentanomial"][i]
                for i in range(5)
            ]

            result["stats"]["wins"] = (
                fastchess_WLD_results["wins"] + saved_stats["wins"]
            )
            result["stats"]["losses"] = (
                fastchess_WLD_results["losses"] + saved_stats["losses"]
            )
            result["stats"]["draws"] = (
                fastchess_WLD_results["draws"] + saved_stats["draws"]
            )

            if spsa_tuning:
                spsa = result["spsa"]
                spsa["wins"] = fastchess_WLD_results["wins"]
                spsa["losses"] = fastchess_WLD_results["losses"]
                spsa["draws"] = fastchess_WLD_results["draws"]

            num_games_finished = fastchess_WLD_results["games"]

            assert (
                2 * sum(result["stats"]["pentanomial"])
                == result["stats"]["wins"]
                + result["stats"]["losses"]
                + result["stats"]["draws"]
            )
            assert num_games_finished == 2 * sum(fastchess_ptnml_results)
            assert num_games_finished <= num_games_updated + batch_size
            assert num_games_finished <= games_to_play

            fastchess_ptnml_results = None
            fastchess_WLD_results = None

            # Send an update_task request after a batch is full or if we have played all games.
            if (num_games_finished == num_games_updated + batch_size) or (
                num_games_finished == games_to_play
            ):
                # Attempt to send game results to the server. Retry a few times upon error.
                update_succeeded = False
                for _ in range(5):
                    try:
                        response = send_api_post_request(
                            remote + "/api/update_task", result
                        )
                        if "error" in response:
                            break
                    except Exception as e:
                        print(
                            "Exception calling update_task:\n",
                            e,
                            sep="",
                            file=sys.stderr,
                        )
                        if isinstance(e, FatalException):  # signal
                            raise e
                    else:
                        if not response["task_alive"]:
                            # This task is no longer necessary
                            print(
                                "The server told us that no more games"
                                " are needed for the current task."
                            )
                            return False
                        update_succeeded = True
                        num_games_updated = num_games_finished
                        break
                    time.sleep(UPDATE_RETRY_TIME)
                if not update_succeeded:
                    raise WorkerException("Too many failed update attempts")
                else:
                    current_state["last_updated"] = datetime.now(timezone.utc)

    else:
        raise WorkerException(
            "{} is past end time {}".format(datetime.now(timezone.utc), end_time)
        )

    return True


def launch_fastchess(
    cmd, current_state, remote, result, spsa_tuning, games_to_play, batch_size, tc_limit
):
    if spsa_tuning:
        # Request parameters for next game.
        req = send_api_post_request(remote + "/api/request_spsa", result)
        if "error" in req:
            raise WorkerException(req["error"])

        if not req["task_alive"]:
            # This task is no longer necessary
            print(
                "The server told us that no more games"
                " are needed for the current task."
            )
            return False

        result["spsa"] = {
            "num_games": games_to_play,
            "wins": 0,
            "losses": 0,
            "draws": 0,
        }

        w_params = req["w_params"]
        b_params = req["b_params"]

    else:
        w_params = []
        b_params = []

    # Run fastchess-cli binary.
    # Stochastic rounding and probability for float N.p: (N, 1-p); (N+1, p)
    idx = cmd.index("_spsa_")
    cmd = (
        cmd[:idx]
        + [
            "option.{}={}".format(
                x["name"], math.floor(x["value"] + random.uniform(0, 1))
            )
            for x in w_params
        ]
        + cmd[idx + 1 :]
    )
    idx = cmd.index("_spsa_")
    cmd = (
        cmd[:idx]
        + [
            "option.{}={}".format(
                x["name"], math.floor(x["value"] + random.uniform(0, 1))
            )
            for x in b_params
        ]
        + cmd[idx + 1 :]
    )

    # print(cmd)
    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
            # The next options are necessary to be able to send a CTRL_C_EVENT to this process.
            # https://stackoverflow.com/questions/7085604/sending-c-to-python-subprocess-objects-on-windows
            startupinfo=(
                subprocess.STARTUPINFO(
                    dwFlags=subprocess.STARTF_USESHOWWINDOW,
                    wShowWindow=subprocess.SW_HIDE,
                )
                if IS_WINDOWS
                else None
            ),
            creationflags=subprocess.CREATE_NEW_CONSOLE if IS_WINDOWS else 0,
            close_fds=not IS_WINDOWS,
        ) as p:
            try:
                task_alive = parse_fastchess_output(
                    p,
                    current_state,
                    remote,
                    result,
                    spsa_tuning,
                    games_to_play,
                    batch_size,
                    tc_limit,
                )
            finally:
                # We nicely ask fastchess to stop.
                try:
                    send_sigint(p)
                except Exception as e:
                    print("\nException in send_sigint:\n", e, sep="", file=sys.stderr)
                # now wait...
                print("\nWaiting for fastchess to finish ... ", end="", flush=True)
                try:
                    p.wait(timeout=FASTCHESS_KILL_TIMEOUT)
                except subprocess.TimeoutExpired:
                    print("timeout", flush=True)
                    kill_process(p)
                else:
                    print("done", flush=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(
            "Exception starting fastchess:\n",
            e,
            sep="",
            file=sys.stderr,
        )
        raise WorkerException("Unable to start fastchess. Error: {}".format(str(e)))

    return task_alive


def run_games(
    worker_info,
    current_state,
    password,
    remote,
    run,
    task_id,
    games_file,
    clear_binaries,
    global_cache,
):
    # This is the main fastchess driver.
    # It is ok, and even expected, for this function to
    # raise exceptions, implicitly or explicitly, if a
    # task cannot be completed.
    # Exceptions will be caught by the caller
    # and handled appropriately.
    # If an immediate exit is necessary then one should
    # raise "FatalException".
    # Explicit exceptions should be raised as
    # "WorkerException". Then they will be recorded
    # on the server.

    task = run["my_task"]

    # Have we run any games on this task yet?

    input_stats = task.get(
        "stats",
        {
            "wins": 0,
            "losses": 0,
            "draws": 0,
            "crashes": 0,
            "time_losses": 0,
            "pentanomial": 5 * [0],
        },
    )
    if "pentanomial" not in input_stats:
        input_stats["pentanomial"] = 5 * [0]

    input_total_games = (
        input_stats["wins"] + input_stats["losses"] + input_stats["draws"]
    )

    assert 2 * sum(input_stats["pentanomial"]) == input_total_games

    input_stats["crashes"] = input_stats.get("crashes", 0)
    input_stats["time_losses"] = input_stats.get("time_losses", 0)

    result = {
        "password": password,
        "run_id": str(run["_id"]),
        "task_id": task_id,
        "stats": input_stats,
        "worker_info": worker_info,
    }

    games_remaining = task["num_games"] - input_total_games

    book = run["args"]["book"]
    book_depth = run["args"]["book_depth"]
    new_options = run["args"]["new_options"]
    base_options = run["args"]["base_options"]
    threads = int(run["args"]["threads"])
    spsa_tuning = "spsa" in run["args"]
    repo_url = run["args"].get("tests_repo")
    worker_concurrency = int(worker_info["concurrency"])
    games_concurrency = worker_concurrency // threads

    if run["args"].get("datagen", False):
        run_datagen_games(
            games_file,
            book,
            worker_concurrency,
            games_remaining,
            run,
            remote,
            worker_info["unique_key"],
            result,
            current_state,
            global_cache,
        )
        return

    assert games_remaining > 0
    assert games_remaining % 2 == 0

    opening_offset = task.get("start", task_id * task["num_games"])
    if "start" in task:
        print("Variable task sizes used. Opening offset = {}".format(opening_offset))
    start_game_index = opening_offset + input_total_games
    run_seed = int(hashlib.sha1(run["_id"].encode("utf-8")).hexdigest(), 16) % (2**64)

    # Format options according to fastchess syntax.
    def parse_options(s):
        results = []
        chunks = s.split("=")
        if len(chunks) == 0:
            return results
        param = chunks[0]
        for c in chunks[1:]:
            val = c.split()
            results.append("option.{}={}".format(param, val[0]))
            param = " ".join(val[1:])
        return results

    new_options = parse_options(new_options)
    base_options = parse_options(base_options)

    # Clean up old engines (keeping the num_bkps most recent).
    worker_dir = Path(__file__).resolve().parent
    testing_dir = worker_dir / "testing"
    num_bkps = 0 if clear_binaries else 50
    try:
        engines = sorted(
            testing_dir.glob("monty_*" + EXE_SUFFIX),
            key=os.path.getmtime,
            reverse=True,
        )
    except Exception as e:
        print(
            "Failed to obtain modification time of old engine binary:\n",
            e,
            sep="",
            file=sys.stderr,
        )
    else:
        for old_engine in engines[num_bkps:]:
            try:
                old_engine.unlink()
            except Exception as e:
                print(
                    "Failed to remove an old engine binary {}:\n".format(old_engine),
                    e,
                    sep="",
                    file=sys.stderr,
                )
    # Create new engines.
    sha_new = run["args"]["resolved_new"]
    sha_base = run["args"]["resolved_base"]
    new_engine_name = "monty_" + sha_new
    base_engine_name = "monty_" + sha_base

    new_engine = testing_dir / new_engine_name
    base_engine = testing_dir / base_engine_name

    # Build from sources new and base engines as needed.
    if not new_engine.with_suffix(EXE_SUFFIX).exists():
        setup_engine(
            new_engine,
            worker_dir,
            testing_dir,
            remote,
            sha_new,
            repo_url,
            global_cache,
        )
    if not base_engine.with_suffix(EXE_SUFFIX).exists():
        setup_engine(
            base_engine,
            worker_dir,
            testing_dir,
            remote,
            sha_base,
            repo_url,
            global_cache,
        )

    os.chdir(testing_dir)

    # Download the opening book if missing in the directory.
    if not (testing_dir / book).exists() or (testing_dir / book).stat().st_size == 0:
        zipball = book + ".zip"
        blob = download_from_github(zipball)
        unzip(blob, testing_dir)

    # Clean up the old networks (keeping the num_bkps most recent)
    num_bkps = 10
    for old_net in sorted(
        testing_dir.glob("nn-*.network"), key=os.path.getmtime, reverse=True
    )[num_bkps:]:
        try:
            old_net.unlink()
        except Exception as e:
            print(
                "Failed to remove an old network {}:\n".format(old_net),
                e,
                sep="",
                file=sys.stderr,
            )

    # PGN files output setup.
    games_name = "results-" + worker_info["unique_key"] + ".pgn"
    games_file[0] = testing_dir / games_name
    games_file = games_file[0]
    try:
        games_file.unlink()
    except FileNotFoundError:
        pass

    # Verify that the signatures are correct.
    run_errors = []
    try:
        base_nps = verify_signature(
            base_engine,
            run["args"]["base_signature"],
            games_concurrency * threads,
        )
    except RunException as e:
        run_errors.append(str(e))
    except WorkerException as e:
        raise e

    if not (
        run["args"]["base_signature"] == run["args"]["new_signature"]
        and new_engine == base_engine
    ):
        try:
            verify_signature(
                new_engine,
                run["args"]["new_signature"],
                games_concurrency * threads,
            )
        except RunException as e:
            run_errors.append(str(e))
        except WorkerException as e:
            raise e

    # Handle exceptions if any.
    if run_errors:
        raise RunException("\n".join(run_errors))

    if base_nps < 61362 / (1 + math.tanh((worker_concurrency - 1) / 8)):
        raise FatalException(
            "This machine is too slow ({} nps / thread) to run montytest effectively - sorry!".format(
                base_nps
            )
        )

    # Value from running bench on 32 processes on Ryzen 9 7950X
    # also set in rundb.py and delta_update_users.py
    factor = BASELINE_NPS / base_nps

    # Adjust CPU scaling.
    _, tc_limit_ltc = adjust_tc("60+0.6", factor)
    scaled_tc, tc_limit = adjust_tc(run["args"]["tc"], factor)
    scaled_new_tc = scaled_tc
    if "new_tc" in run["args"]:
        scaled_new_tc, new_tc_limit = adjust_tc(run["args"]["new_tc"], factor)
        tc_limit = (tc_limit + new_tc_limit) / 2

    result["worker_info"]["nps"] = float(base_nps)

    threads_cmd = []
    if not any("Threads" in s for s in new_options + base_options):
        threads_cmd = ["option.Threads={}".format(threads)]

    # If nodestime is being used, give engines extra grace time to
    # make time losses virtually impossible.
    nodestime_cmd = []
    if any("nodestime" in s for s in new_options + base_options):
        nodestime_cmd = ["timemargin=10000"]

    def make_player(arg):
        return run["args"][arg].split(" ")[0]

    if spsa_tuning:
        tc_limit *= 2

    while games_remaining > 0:
        # Update frequency for NumGames/SPSA test:
        # every 4 games at LTC, or a similar time interval at shorter TCs
        batch_size = games_concurrency * 4 * max(1, round(tc_limit_ltc / tc_limit))

        if spsa_tuning:
            games_to_play = min(batch_size, games_remaining)
            pgnout = []
        else:
            games_to_play = games_remaining
            pgnout = ["-pgnout", games_name]

        if "sprt" in run["args"]:
            batch_size = 2 * run["args"]["sprt"].get("batch_size", 1)
            assert games_to_play % batch_size == 0

        assert batch_size % 2 == 0
        assert games_to_play % 2 == 0

        # Handle book or PGN file.
        pgn_cmd = []
        book_cmd = []
        if int(book_depth) <= 0:
            pass
        elif book.endswith(".pgn") or book.endswith(".epd"):
            plies = 2 * int(book_depth)
            pgn_cmd = [
                "-openings",
                "file={}".format(book),
                "format={}".format(book[-3:]),
                "order=random",
                "plies={}".format(plies),
                "start={}".format(1 + start_game_index // 2),
            ]
        else:
            assert False

        # Check for an FRC/Chess960 opening book
        variant = "standard"
        if any(substring in book.upper() for substring in ["FRC", "960"]):
            variant = "fischerandom"

        # Run fastchess binary.
        fastchess = "fastchess" + EXE_SUFFIX
        cmd = (
            [
                os.path.join(testing_dir, fastchess),
                "-recover",
                "-repeat",
                "-games",
                "2",
                "-rounds",
                str(int(games_to_play) // 2),
                "-tournament",
                "gauntlet",
            ]
            + [
                "-ratinginterval",
                "1",
                "-scoreinterval",
                "1",
                "-autosaveinterval",
                "0",
                "-report",
                "penta=true",
            ]
            + pgnout
            + ["-site", "https://tests.montychess.org/tests/view/" + run["_id"]]
            + [
                "-event",
                "Batch {}: {} vs {}".format(
                    task_id, make_player("new_tag"), make_player("base_tag")
                ),
            ]
            + ["-srand", "{}".format(run_seed)]
            + (
                [
                    "-resign",
                    "movecount=3",
                    "score=600",
                    "-draw",
                    "movenumber=34",
                    "movecount=8",
                    "score=20",
                ]
                if run["args"].get("adjudication", True)
                else []
            )
            + ["-variant", "{}".format(variant)]
            + [
                "-concurrency",
                str(int(games_concurrency)),
            ]
            + pgn_cmd
            + [
                "-engine",
                "name=New-" + run["args"]["resolved_new"],
                "tc={}".format(scaled_new_tc),
                "cmd=./{}".format(new_engine_name),
                "dir=.",
            ]
            + new_options
            + ["_spsa_"]
            + [
                "-engine",
                "name=Base-" + run["args"]["resolved_base"],
                "tc={}".format(scaled_tc),
                "cmd=./{}".format(base_engine_name),
                "dir=.",
            ]
            + base_options
            + ["_spsa_"]
            + ["-each", "proto=uci"]
            + nodestime_cmd
            + threads_cmd
            + book_cmd
        )

        task_alive = launch_fastchess(
            cmd,
            current_state,
            remote,
            result,
            spsa_tuning,
            games_to_play,
            batch_size,
            tc_limit * max(8, games_to_play / games_concurrency),
        )

        games_remaining -= games_to_play
        start_game_index += games_to_play

        if not task_alive:
            break

    return


def send_datagen_result(result, remote, current_state):
    update_succeeded = False
    for _ in range(5):
        try:
            response = send_api_post_request(remote + "/api/update_task", result)
            if "error" in response:
                break
        except Exception as e:
            print(
                "Exception calling update_task:\n",
                e,
                sep="",
                file=sys.stderr,
            )
            if isinstance(e, FatalException):  # signal
                raise e
        else:
            if not response["task_alive"]:
                # This task is no longer necessary
                print(
                    "The server told us that no more games"
                    " are needed for the current task."
                )
                return False
            update_succeeded = True
            break
        time.sleep(UPDATE_RETRY_TIME)
    if not update_succeeded:
        raise WorkerException("Too many failed update attempts")
    else:
        current_state["last_updated"] = datetime.now(timezone.utc)


def parse_datagen_output(p, tc_factor, result):
    saved_stats = copy.deepcopy(result["stats"])

    q = Queue()
    t_output = threading.Thread(target=enqueue_output, args=(p.stdout, q), daemon=True)
    t_output.start()
    t_error = threading.Thread(target=enqueue_output, args=(p.stderr, q), daemon=True)
    t_error.start()

    tc_limit = tc_factor * 1800 * 2  # Allow a factor of two to account for variance
    end_time = datetime.now(timezone.utc) + timedelta(seconds=tc_limit)
    print("TC limit {} End time: {}".format(tc_limit, end_time))

    while datetime.now(timezone.utc) < end_time:
        try:
            line = q.get_nowait().strip()
        except Empty:
            if p.poll() is not None:
                break
            time.sleep(1)
            continue

        print(line, flush=True)

        if "finished games" in line:
            # Parsing sometimes fails. We want to understand why.
            try:
                chunks = line.split(" ")
                wld = [int(chunks[8]), int(chunks[4]), int(chunks[6])]
            except:
                raise WorkerException("Failed to parse score line: {}".format(line))
    else:
        raise WorkerException(
            "{} is past end time {}".format(datetime.now(timezone.utc), end_time)
        )

    result["stats"]["wins"] = wld[0] + saved_stats["wins"]
    result["stats"]["losses"] = wld[1] + saved_stats["losses"]
    result["stats"]["draws"] = wld[2] + saved_stats["draws"]

    wins = result["stats"]["wins"]
    draws = result["stats"]["draws"]
    losses = result["stats"]["losses"]

    winLossDiff = wins - losses

    result["stats"]["pentanomial"][1] = max(-winLossDiff, 0)
    result["stats"]["pentanomial"][2] = int(
        (wins + draws + losses) / 2 - abs(winLossDiff)
    )
    result["stats"]["pentanomial"][3] = max(winLossDiff, 0)

    return result


def run_datagen_games(
    games_file,
    book,
    threads,
    games,
    run,
    remote,
    key,
    result,
    current_state,
    global_cache,
):
    sha_new = run["args"]["resolved_new"]
    new_engine_name = "monty_datagen_" + sha_new

    repo_url = run["args"].get("tests_repo")
    worker_dir = Path(__file__).resolve().parent
    testing_dir = worker_dir / "testing"

    new_engine = testing_dir / new_engine_name

    # Build from sources new and base engines as needed.
    if not new_engine.with_suffix(EXE_SUFFIX).exists():
        setup_engine(
            new_engine,
            worker_dir,
            testing_dir,
            remote,
            sha_new,
            repo_url,
            global_cache,
            datagen=True,
        )

    os.chdir(testing_dir)

    # Download the opening book if missing in the directory.
    if not (testing_dir / book).exists() or (testing_dir / book).stat().st_size == 0:
        zipball = book + ".zip"
        blob = download_from_github(zipball)
        unzip(blob, testing_dir)

    # Verify that the signatures are correct.
    run_errors = []
    try:
        nps = verify_signature(
            new_engine,
            run["args"]["base_signature"],
            threads,
        )
        tc_factor = BASELINE_NPS / (nps / 4)
        result["worker_info"]["nps"] = nps
    except RunException as e:
        run_errors.append(str(e))
    except WorkerException as e:
        raise e

    # Handle exceptions if any.
    if run_errors:
        raise RunException("\n".join(run_errors))

    games_name = "data-" + key + ".binpack"
    games_file[0] = testing_dir / games_name
    games_file = games_file[0]
    try:
        games_file.unlink()
    except FileNotFoundError:
        pass

    nodes = run["args"]["nodes"]

    cmd = [
        new_engine,
        "-o",
        games_name,
        "-n",
        str(nodes),
        "-t",
        str(threads),
        "-g",
        str(games),
    ]

    if book is not None and book.endswith(".epd"):
        cmd.append("-b")
        cmd.append(book)

    try:
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            bufsize=1,
            # The next options are necessary to be able to send a CTRL_C_EVENT to this process.
            # https://stackoverflow.com/questions/7085604/sending-c-to-python-subprocess-objects-on-windows
            startupinfo=(
                subprocess.STARTUPINFO(
                    dwFlags=subprocess.STARTF_USESHOWWINDOW,
                    wShowWindow=subprocess.SW_HIDE,
                )
                if IS_WINDOWS
                else None
            ),
            creationflags=subprocess.CREATE_NEW_CONSOLE if IS_WINDOWS else 0,
            close_fds=not IS_WINDOWS,
        ) as p:
            try:
                result = parse_datagen_output(p, tc_factor, result)
            except:
                # Remove the binpack on exception
                print("Removing binpack", flush=True)
                if games_file.exists():
                    games_file.unlink()
            finally:
                # We nicely ask cutechess-cli to stop.
                try:
                    send_sigint(p)
                except Exception as e:
                    print("\nException in send_sigint:\n", e, sep="", file=sys.stderr)
                # now wait...
                print("\nWaiting for datagen to finish ... ", end="", flush=True)
                try:
                    p.wait(timeout=FASTCHESS_KILL_TIMEOUT)
                    # Check the return code of the process
                    if p.returncode != 0:
                        print(
                            f"Datagen process exited with code {p.returncode}. Removing binpack.",
                            flush=True,
                        )
                        if games_file.exists():
                            games_file.unlink()
                        raise WorkerException(
                            f"Datagen process exited with non-zero return code: {p.returncode}"
                        )
                    else:
                        send_datagen_result(result, remote, current_state)
                except subprocess.TimeoutExpired:
                    print("timeout", flush=True)
                    kill_process(p)
                else:
                    print("done", flush=True)
    except (OSError, subprocess.SubprocessError) as e:
        print(
            "Exception starting datagen:\n",
            e,
            sep="",
            file=sys.stderr,
        )
        raise WorkerException("Unable to start datagen. Error: {}".format(str(e)))

    return
