from montytest.rundb import RunDb


def get_rundb():
    return RunDb(db_name="montytest_tests")


def find_run(arg="username", value="travis"):
    rundb = RunDb(db_name="montytest_tests")
    for run in rundb.get_unfinished_runs():
        if run["args"][arg] == value:
            return run
    return None
