#!/usr/bin/env python3

import argparse
import copy
from datetime import datetime
from distutils.util import strtobool
from distutils.version import LooseVersion
import functools
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile

import torch
from torch.utils import cpp_extension
from torch.testing._internal.common_utils import (
    FILE_SCHEMA,
    IS_IN_CI,
    TEST_WITH_ROCM,
    shell,
    set_cwd,
    parser as common_parser,
)
import torch.distributed as dist
from typing import Dict, Optional, List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

try:
    # using tools/ to optimize test run.
    sys.path.append(str(REPO_ROOT))
    from tools.testing.test_selections import (
        export_S3_test_times,
        get_shard_based_on_S3,
        # NS: Disable target determination
        # get_slow_tests_based_on_S3,
        get_specified_test_cases,
        get_reordered_tests,
        get_test_case_configs,
    )
    # NS: Disable target determination
    # from tools.testing.modulefinder_determinator import (
    #     should_run_test,
    #     TARGET_DET_LIST,
    # )

    HAVE_TEST_SELECTION_TOOLS = True
except ImportError:
    HAVE_TEST_SELECTION_TOOLS = False
    print(
        "Unable to import test_selections from tools/testing. Running without test selection stats..."
    )


def discover_tests(
        base_dir: Optional[pathlib.Path] = None,
        blocklisted_patterns: Optional[List[str]] = None,
        blocklisted_tests: Optional[List[str]] = None,
        extra_tests: Optional[List[str]] = None) -> List[str]:
    """
    Searches for all python files starting with test_ excluding one specified by patterns
    """
    def skip_test_p(name: str) -> bool:
        rc = False
        if blocklisted_patterns is not None:
            rc |= any(name.startswith(pattern) for pattern in blocklisted_patterns)
        if blocklisted_tests is not None:
            rc |= name in blocklisted_tests
        return rc
    cwd = pathlib.Path(__file__).resolve().parent if base_dir is None else base_dir
    all_py_files = list(cwd.glob('**/test_*.py'))
    rc = [str(fname.relative_to(cwd))[:-3] for fname in all_py_files]
    # Invert slashes on Windows
    if sys.platform == "win32":
        rc = [name.replace('\\', '/') for name in rc]
    rc = [test for test in rc if not skip_test_p(test)]
    if extra_tests is not None:
        rc += extra_tests
    return sorted(rc)

TESTS = discover_tests(
    blocklisted_patterns=[
        'ao',
        'bottleneck_test',
        'custom_backend',
        'custom_operator',
        'fx',        # executed by test_fx.py
        'jit',      # executed by test_jit.py
        'mobile',
        'onnx',
        'package',  # executed by test_package.py
        'quantization',  # executed by test_quantization.py
        'autograd',  # executed by test_autograd.py
    ],
    blocklisted_tests=[
        'test_bundled_images',
        'test_cpp_extensions_aot',
        'test_determination',
        'test_jit_fuser',
        'test_jit_simple',
        'test_jit_string',
        'test_kernel_launch_checks',
        'test_metal',
        'test_nnapi',
        'test_segment_reductions',
        'test_static_runtime',
        'test_throughput_benchmark',
        'test_typing',
        "distributed/algorithms/ddp_comm_hooks/test_ddp_hooks",
        "distributed/algorithms/quantization/test_quantization",
        "distributed/bin/test_script",
        "distributed/elastic/multiprocessing/bin/test_script",
        "distributed/launcher/bin/test_script",
        "distributed/launcher/bin/test_script_init_method",
        "distributed/launcher/bin/test_script_is_torchelastic_launched",
        "distributed/launcher/bin/test_script_local_rank",
        "distributed/test_c10d_spawn",
        'distributions/test_transforms',
        'distributions/test_utils',
    ],
    extra_tests=[
        "test_cpp_extensions_aot_ninja",
        "test_cpp_extensions_aot_no_ninja",
        "distributed/elastic/timer/api_test",
        "distributed/elastic/timer/local_timer_example",
        "distributed/elastic/timer/local_timer_test",
        "distributed/elastic/events/lib_test",
        "distributed/elastic/metrics/api_test",
        "distributed/elastic/utils/logging_test",
        "distributed/elastic/utils/util_test",
        "distributed/elastic/utils/distributed_test",
        "distributed/elastic/multiprocessing/api_test",
        "test_deploy",
    ]
)

FSDP_TEST = [test for test in TESTS if test.startswith("distributed/fsdp")]

# Tests need to be run with pytest.
USE_PYTEST_LIST = [
    "distributed/pipeline/sync/skip/test_api",
    "distributed/pipeline/sync/skip/test_gpipe",
    "distributed/pipeline/sync/skip/test_inspect_skip_layout",
    "distributed/pipeline/sync/skip/test_leak",
    "distributed/pipeline/sync/skip/test_portal",
    "distributed/pipeline/sync/skip/test_stash_pop",
    "distributed/pipeline/sync/skip/test_tracker",
    "distributed/pipeline/sync/skip/test_verify_skippables",
    "distributed/pipeline/sync/test_balance",
    "distributed/pipeline/sync/test_bugs",
    "distributed/pipeline/sync/test_checkpoint",
    "distributed/pipeline/sync/test_copy",
    "distributed/pipeline/sync/test_deferred_batch_norm",
    "distributed/pipeline/sync/test_dependency",
    "distributed/pipeline/sync/test_inplace",
    "distributed/pipeline/sync/test_microbatch",
    "distributed/pipeline/sync/test_phony",
    "distributed/pipeline/sync/test_pipe",
    "distributed/pipeline/sync/test_pipeline",
    "distributed/pipeline/sync/test_stream",
    "distributed/pipeline/sync/test_transparency",
    "distributed/pipeline/sync/test_worker",
    "distributions/test_constraints",
    "distributions/test_transforms",
    "distributions/test_utils",
    "test_typing",
    "distributed/elastic/events/lib_test",
    "distributed/elastic/agent/server/test/api_test",
    "test_deploy",
]

WINDOWS_BLOCKLIST = [
    "distributed/nn/jit/test_instantiator",
    "distributed/rpc/test_faulty_agent",
    "distributed/rpc/test_tensorpipe_agent",
    "distributed/rpc/test_share_memory",
    "distributed/rpc/cuda/test_tensorpipe_agent",
    "distributed/pipeline/sync/skip/test_api",
    "distributed/pipeline/sync/skip/test_gpipe",
    "distributed/pipeline/sync/skip/test_inspect_skip_layout",
    "distributed/pipeline/sync/skip/test_leak",
    "distributed/pipeline/sync/skip/test_portal",
    "distributed/pipeline/sync/skip/test_stash_pop",
    "distributed/pipeline/sync/skip/test_tracker",
    "distributed/pipeline/sync/skip/test_verify_skippables",
    "distributed/pipeline/sync/test_balance",
    "distributed/pipeline/sync/test_bugs",
    "distributed/pipeline/sync/test_checkpoint",
    "distributed/pipeline/sync/test_copy",
    "distributed/pipeline/sync/test_deferred_batch_norm",
    "distributed/pipeline/sync/test_dependency",
    "distributed/pipeline/sync/test_inplace",
    "distributed/pipeline/sync/test_microbatch",
    "distributed/pipeline/sync/test_phony",
    "distributed/pipeline/sync/test_pipe",
    "distributed/pipeline/sync/test_pipeline",
    "distributed/pipeline/sync/test_stream",
    "distributed/pipeline/sync/test_transparency",
    "distributed/pipeline/sync/test_worker",
    "distributed/elastic/agent/server/test/api_test",
    "distributed/elastic/multiprocessing/api_test",
    "distributed/_shard/checkpoint/test_checkpoint"
    "distributed/_shard/checkpoint/test_file_system_checkpoint"
    "distributed/_shard/sharding_spec/test_sharding_spec",
    "distributed/_shard/sharding_plan/test_sharding_plan",
    "distributed/_shard/sharded_tensor/test_megatron_prototype",
    "distributed/_shard/sharded_tensor/test_sharded_tensor",
    "distributed/_shard/sharded_tensor/test_sharded_tensor_reshard",
    "distributed/_shard/sharded_tensor/ops/test_chunk",
    "distributed/_shard/sharded_tensor/ops/test_elementwise_ops",
    "distributed/_shard/sharded_tensor/ops/test_embedding",
    "distributed/_shard/sharded_tensor/ops/test_embedding_bag",
    "distributed/_shard/sharded_tensor/ops/test_binary_cmp",
    "distributed/_shard/sharded_tensor/ops/test_init",
    "distributed/_shard/sharded_tensor/ops/test_linear",
    "distributed/_shard/sharded_tensor/ops/test_math_ops",
    "distributed/_shard/sharded_tensor/ops/test_matrix_ops",
    "distributed/_shard/sharded_tensor/ops/test_softmax",
    "distributed/_shard/sharded_optim/test_sharded_optim",
    "distributed/_shard/test_partial_tensor",
    "distributed/_shard/test_replicated_tensor",
] + FSDP_TEST

ROCM_BLOCKLIST = [
    "distributed/rpc/test_faulty_agent",
    "distributed/rpc/test_tensorpipe_agent",
    "distributed/rpc/test_share_memory",
    "distributed/rpc/cuda/test_tensorpipe_agent",
    "distributed/_shard/checkpoint/test_checkpoint"
    "distributed/_shard/checkpoint/test_file_system_checkpoint"
    "distributed/_shard/sharding_spec/test_sharding_spec",
    "distributed/_shard/sharding_plan/test_sharding_plan",
    "distributed/_shard/sharded_tensor/test_megatron_prototype",
    "distributed/_shard/sharded_tensor/test_sharded_tensor",
    "distributed/_shard/sharded_tensor/test_sharded_tensor_reshard",
    "distributed/_shard/sharded_tensor/ops/test_chunk",
    "distributed/_shard/sharded_tensor/ops/test_elementwise_ops",
    "distributed/_shard/sharded_tensor/ops/test_embedding",
    "distributed/_shard/sharded_tensor/ops/test_embedding_bag",
    "distributed/_shard/sharded_tensor/ops/test_binary_cmp",
    "distributed/_shard/sharded_tensor/ops/test_init",
    "distributed/_shard/sharded_tensor/ops/test_linear",
    "distributed/_shard/sharded_tensor/ops/test_math_ops",
    "distributed/_shard/sharded_tensor/ops/test_matrix_ops",
    "distributed/_shard/sharded_tensor/ops/test_softmax",
    "distributed/_shard/sharded_optim/test_sharded_optim",
    "distributed/_shard/test_partial_tensor",
    "distributed/_shard/test_replicated_tensor",
    "test_determination",
    "test_jit_legacy",
    "test_openmp",
]

RUN_PARALLEL_BLOCKLIST = [
    "test_cpp_extensions_jit",
    "test_jit_disabled",
    "test_mobile_optimizer",
    "test_multiprocessing",
    "test_multiprocessing_spawn",
    "test_namedtuple_return_api",
    "test_overrides",
    "test_show_pickle",
    "test_tensorexpr",
    "test_cuda_primary_ctx",
] + FSDP_TEST

WINDOWS_COVERAGE_BLOCKLIST = []

# A subset of our TEST list that validates PyTorch's ops, modules, and autograd function as expected
CORE_TEST_LIST = [
    "test_autograd",
    "test_modules",
    "test_nn",
    "test_ops",
    "test_ops_gradients",
    "test_ops_jit",
    "test_torch"
]

# the JSON file to store the S3 test stats
TEST_TIMES_FILE = ".pytorch-test-times.json"

# if a test file takes longer than 5 min, we add it to TARGET_DET_LIST
SLOW_TEST_THRESHOLD = 300

DISTRIBUTED_TESTS_CONFIG = {}


if dist.is_available():
    DISTRIBUTED_TESTS_CONFIG["test"] = {"WORLD_SIZE": "1"}
    if not TEST_WITH_ROCM and dist.is_mpi_available():
        DISTRIBUTED_TESTS_CONFIG["mpi"] = {
            "WORLD_SIZE": "3",
            "TEST_REPORT_SOURCE_OVERRIDE": "dist-mpi",
        }
    if dist.is_nccl_available():
        DISTRIBUTED_TESTS_CONFIG["nccl"] = {
            "WORLD_SIZE": "2" if torch.cuda.device_count() == 2 else "3",
            "TEST_REPORT_SOURCE_OVERRIDE": "dist-nccl",
        }
    if dist.is_gloo_available():
        DISTRIBUTED_TESTS_CONFIG["gloo"] = {
            "WORLD_SIZE": "2" if torch.cuda.device_count() == 2 else "3",
            "TEST_REPORT_SOURCE_OVERRIDE": "dist-gloo",
        }

# https://stackoverflow.com/questions/2549939/get-signal-names-from-numbers-in-python
SIGNALS_TO_NAMES_DICT = {
    getattr(signal, n): n for n in dir(signal) if n.startswith("SIG") and "_" not in n
}

CPP_EXTENSIONS_ERROR = """
Ninja (https://ninja-build.org) is required for some of the C++ extensions
tests, but it could not be found. Install ninja with `pip install ninja`
or `conda install ninja`. Alternatively, disable said tests with
`run_test.py --exclude test_cpp_extensions_aot_ninja test_cpp_extensions_jit`.
"""

PYTORCH_COLLECT_COVERAGE = bool(os.environ.get("PYTORCH_COLLECT_COVERAGE"))

ENABLE_PR_HISTORY_REORDERING = bool(
    os.environ.get("ENABLE_PR_HISTORY_REORDERING", "0") == "1"
)

JIT_EXECUTOR_TESTS = [
    "test_jit_profiling",
    "test_jit_legacy",
    "test_jit_fuser_legacy",
]

DISTRIBUTED_TESTS = [test for test in TESTS if test.startswith("distributed")]

TESTS_REQUIRING_LAPACK = [
    "distributions/test_constraints",
    "distributions/test_distributions",
]

# Dictionary matching test modules (in TESTS) to lists of test cases (within that test_module) that would be run when
# options.run_specified_test_cases is enabled.
# For example:
# {
#   "test_nn": ["test_doubletensor_avg_pool3d", "test_share_memory", "test_hook_requires_grad"],
#   ...
# }
# then for test_nn.py, we would ONLY run test_doubletensor_avg_pool3d, test_share_memory, and test_hook_requires_grad.
SPECIFIED_TEST_CASES_DICT: Dict[str, List[str]] = {}

# The file from which the SPECIFIED_TEST_CASES_DICT will be filled, a CSV of test cases that would be run when
# options.run_specified_test_cases is enabled.
SPECIFIED_TEST_CASES_FILE: str = ".pytorch_specified_test_cases.csv"


def print_to_stderr(message):
    print(message, file=sys.stderr)


def get_test_case_args(test_module, using_pytest) -> List[str]:
    args = []
    # if test_module not specified or specified with '__all__' then run all tests
    if (
        test_module not in SPECIFIED_TEST_CASES_DICT
        or "__all__" in SPECIFIED_TEST_CASES_DICT[test_module]
    ):
        return args

    if using_pytest:
        args.append("-k")
        args.append(" or ".join(SPECIFIED_TEST_CASES_DICT[test_module]))
    else:
        for test in SPECIFIED_TEST_CASES_DICT[test_module]:
            args.append("-k")
            args.append(test)

    return args


def get_executable_command(options, allow_pytest, disable_coverage=False):
    if options.coverage and not disable_coverage:
        executable = ["coverage", "run", "--parallel-mode", "--source=torch"]
    else:
        executable = [sys.executable]
    if options.pytest:
        if allow_pytest:
            executable += ["-m", "pytest"]
        else:
            print_to_stderr(
                "Pytest cannot be used for this test. Falling back to unittest."
            )
    return executable


def run_test(
    test_module, test_directory, options, launcher_cmd=None, extra_unittest_args=None
):
    unittest_args = options.additional_unittest_args.copy()
    if options.verbose:
        unittest_args.append(f'-{"v"*options.verbose}')  # in case of pytest
    if test_module in RUN_PARALLEL_BLOCKLIST:
        unittest_args = [
            arg for arg in unittest_args if not arg.startswith("--run-parallel")
        ]
    if extra_unittest_args:
        assert isinstance(extra_unittest_args, list)
        unittest_args.extend(extra_unittest_args)

    # If using pytest, replace -f with equivalent -x
    if options.pytest:
        unittest_args = [arg if arg != "-f" else "-x" for arg in unittest_args]
    elif IS_IN_CI:
        # use the downloaded test cases configuration, not supported in pytest
        unittest_args.extend(["--import-slow-tests", "--import-disabled-tests"])

    # Multiprocessing related tests cannot run with coverage.
    # Tracking issue: https://github.com/pytorch/pytorch/issues/50661
    disable_coverage = (
        sys.platform == "win32" and test_module in WINDOWS_COVERAGE_BLOCKLIST
    )

    # Extra arguments are not supported with pytest
    executable = get_executable_command(
        options, allow_pytest=not extra_unittest_args, disable_coverage=disable_coverage
    )

    # TODO: move this logic into common_utils.py instead of passing in "-k" individually
    # The following logic for running specified tests will only run for non-distributed tests, as those are dispatched
    # to test_distributed and not run_test (this function)
    if options.run_specified_test_cases:
        unittest_args.extend(get_test_case_args(test_module, "pytest" in executable))

    # Can't call `python -m unittest test_*` here because it doesn't run code
    # in `if __name__ == '__main__': `. So call `python test_*.py` instead.
    argv = [test_module + ".py"] + unittest_args

    command = (launcher_cmd or []) + executable + argv
    print_to_stderr("Executing {} ... [{}]".format(command, datetime.now()))
    return shell(command, test_directory)


def test_cuda_primary_ctx(test_module, test_directory, options):
    return run_test(
        test_module, test_directory, options, extra_unittest_args=["--subprocess"]
    )

run_test_with_subprocess = functools.partial(run_test, extra_unittest_args=["--subprocess"])


def get_run_test_with_subprocess_fn():
    return lambda test_module, test_directory, options: run_test_with_subprocess(test_module, test_directory, options)



def _test_cpp_extensions_aot(test_directory, options, use_ninja):
    if use_ninja:
        try:
            cpp_extension.verify_ninja_availability()
        except RuntimeError:
            print(CPP_EXTENSIONS_ERROR)
            return 1

    # Wipe the build folder, if it exists already
    cpp_extensions_test_dir = os.path.join(test_directory, "cpp_extensions")
    cpp_extensions_test_build_dir = os.path.join(cpp_extensions_test_dir, "build")
    if os.path.exists(cpp_extensions_test_build_dir):
        shutil.rmtree(cpp_extensions_test_build_dir)

    # Build the test cpp extensions modules
    shell_env = os.environ.copy()
    shell_env["USE_NINJA"] = str(1 if use_ninja else 0)
    cmd = [sys.executable, "setup.py", "install", "--root", "./install"]
    return_code = shell(cmd, cwd=cpp_extensions_test_dir, env=shell_env)
    if return_code != 0:
        return return_code
    if sys.platform != "win32":
        return_code = shell(
            cmd,
            cwd=os.path.join(cpp_extensions_test_dir, "no_python_abi_suffix_test"),
            env=shell_env,
        )
        if return_code != 0:
            return return_code

    # "install" the test modules and run tests
    python_path = os.environ.get("PYTHONPATH", "")
    from shutil import copyfile

    os.environ['USE_NINJA'] = shell_env['USE_NINJA']
    test_module = "test_cpp_extensions_aot" + ("_ninja" if use_ninja else "_no_ninja")
    copyfile(
        test_directory + "/test_cpp_extensions_aot.py",
        test_directory + "/" + test_module + ".py",
    )
    try:
        cpp_extensions = os.path.join(test_directory, "cpp_extensions")
        install_directory = ""
        # install directory is the one that is named site-packages
        for root, directories, _ in os.walk(os.path.join(cpp_extensions, "install")):
            for directory in directories:
                if "-packages" in directory:
                    install_directory = os.path.join(root, directory)

        assert install_directory, "install_directory must not be empty"
        os.environ["PYTHONPATH"] = os.pathsep.join([install_directory, python_path])
        return run_test(test_module, test_directory, options)
    finally:
        os.environ["PYTHONPATH"] = python_path
        if os.path.exists(test_directory + "/" + test_module + ".py"):
            os.remove(test_directory + "/" + test_module + ".py")
        os.environ.pop('USE_NINJA')


def test_cpp_extensions_aot_ninja(test_module, test_directory, options):
    return _test_cpp_extensions_aot(test_directory, options, use_ninja=True)


def test_cpp_extensions_aot_no_ninja(test_module, test_directory, options):
    return _test_cpp_extensions_aot(test_directory, options, use_ninja=False)


def test_distributed(test_module, test_directory, options):
    # MPI tests are broken with Python-3.9
    mpi_available = subprocess.call(
        "command -v mpiexec", shell=True
    ) == 0 and sys.version_info < (3, 9)
    if options.verbose and not mpi_available:
        print_to_stderr("MPI not available -- MPI backend tests will be skipped")
    config = DISTRIBUTED_TESTS_CONFIG
    for backend, env_vars in config.items():
        if sys.platform == "win32" and backend != "gloo":
            continue
        if backend == "mpi" and not mpi_available:
            continue
        for with_init_file in {True, False}:
            if sys.platform == "win32" and not with_init_file:
                continue
            tmp_dir = tempfile.mkdtemp()
            if options.verbose:
                init_str = "with {} init_method"
                with_init = init_str.format("file" if with_init_file else "env")
                print_to_stderr(
                    "Running distributed tests for the {} backend {}".format(
                        backend, with_init
                    )
                )
            old_environ = dict(os.environ)
            os.environ["TEMP_DIR"] = tmp_dir
            os.environ["BACKEND"] = backend
            os.environ["INIT_METHOD"] = "env://"
            os.environ.update(env_vars)
            if with_init_file:
                if test_module == "test_distributed_spawn":
                    init_method = f"{FILE_SCHEMA}{tmp_dir}/"
                else:
                    init_method = f"{FILE_SCHEMA}{tmp_dir}/shared_init_file"
                os.environ["INIT_METHOD"] = init_method
            try:
                os.mkdir(os.path.join(tmp_dir, "barrier"))
                os.mkdir(os.path.join(tmp_dir, "test_dir"))
                if backend == "mpi":
                    # test mpiexec for --noprefix option
                    with open(os.devnull, "w") as devnull:
                        allowrunasroot_opt = (
                            "--allow-run-as-root"
                            if subprocess.call(
                                'mpiexec --allow-run-as-root -n 1 bash -c ""',
                                shell=True,
                                stdout=devnull,
                                stderr=subprocess.STDOUT,
                            )
                            == 0
                            else ""
                        )
                        noprefix_opt = (
                            "--noprefix"
                            if subprocess.call(
                                f'mpiexec {allowrunasroot_opt} -n 1 --noprefix bash -c ""',
                                shell=True,
                                stdout=devnull,
                                stderr=subprocess.STDOUT,
                            )
                            == 0
                            else ""
                        )

                    mpiexec = ["mpiexec", "-n", "3", noprefix_opt, allowrunasroot_opt]

                    return_code = run_test(
                        test_module, test_directory, options, launcher_cmd=mpiexec
                    )
                else:
                    return_code = run_test(test_module, test_directory, options, extra_unittest_args=["--subprocess"])
                if return_code != 0:
                    return return_code
            finally:
                shutil.rmtree(tmp_dir)
                os.environ.clear()
                os.environ.update(old_environ)
    return 0


CUSTOM_HANDLERS = {
    "test_cuda_primary_ctx": test_cuda_primary_ctx,
    "test_cpp_extensions_aot_no_ninja": test_cpp_extensions_aot_no_ninja,
    "test_cpp_extensions_aot_ninja": test_cpp_extensions_aot_ninja,
    "distributed/test_distributed_spawn": test_distributed,
    "distributed/test_c10d_nccl": get_run_test_with_subprocess_fn(),
    "distributed/test_c10d_gloo": get_run_test_with_subprocess_fn(),
    "distributed/test_c10d_common": get_run_test_with_subprocess_fn(),
    "distributed/test_c10d_spawn_gloo": get_run_test_with_subprocess_fn(),
    "distributed/test_c10d_spawn_nccl": get_run_test_with_subprocess_fn(),
    "distributed/test_store": get_run_test_with_subprocess_fn(),
    "distributed/test_pg_wrapper": get_run_test_with_subprocess_fn(),
    "distributed/rpc/test_faulty_agent": get_run_test_with_subprocess_fn(),
    "distributed/rpc/test_tensorpipe_agent": get_run_test_with_subprocess_fn(),
    "distributed/rpc/test_share_memory": get_run_test_with_subprocess_fn(),
    "distributed/rpc/cuda/test_tensorpipe_agent": get_run_test_with_subprocess_fn(),
}

def parse_test_module(test):
    return test.split(".")[0]


class TestChoices(list):
    def __init__(self, *args, **kwargs):
        super(TestChoices, self).__init__(args[0])

    def __contains__(self, item):
        return list.__contains__(self, parse_test_module(item))


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the PyTorch unit test suite",
        epilog="where TESTS is any of: {}".format(", ".join(TESTS)),
        formatter_class=argparse.RawTextHelpFormatter,
        parents=[common_parser]
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="print verbose information and test-by-test results",
    )
    parser.add_argument("--jit", "--jit", action="store_true", help="run all jit tests")
    parser.add_argument(
        "--distributed-tests",
        "--distributed-tests",
        action="store_true",
        help="run all distributed tests",
    )
    parser.add_argument(
        "-core",
        "--core",
        action="store_true",
        help="Only run core tests, or tests that validate PyTorch's ops, modules,"
        "and autograd. They are defined by CORE_TEST_LIST."
    )
    parser.add_argument(
        "-pt",
        "--pytest",
        action="store_true",
        help="If true, use `pytest` to execute the tests. E.g., this runs "
        "TestTorch with pytest in verbose and coverage mode: "
        "python run_test.py -vci torch -pt",
    )
    parser.add_argument(
        "-c",
        "--coverage",
        action="store_true",
        help="enable coverage",
        default=PYTORCH_COLLECT_COVERAGE,
    )
    parser.add_argument(
        "-i",
        "--include",
        nargs="+",
        choices=TestChoices(TESTS),
        default=TESTS,
        metavar="TESTS",
        help="select a set of tests to include (defaults to ALL tests)."
        " tests must be a part of the TESTS list defined in run_test.py",
    )
    parser.add_argument(
        "-x",
        "--exclude",
        nargs="+",
        choices=TESTS,
        metavar="TESTS",
        default=[],
        help="select a set of tests to exclude",
    )
    parser.add_argument(
        "-f",
        "--first",
        choices=TESTS,
        metavar="TESTS",
        help="select the test to start from (excludes previous tests)",
    )
    parser.add_argument(
        "-l",
        "--last",
        choices=TESTS,
        metavar="TESTS",
        help="select the last test to run (excludes following tests)",
    )
    parser.add_argument(
        "--bring-to-front",
        nargs="+",
        choices=TestChoices(TESTS),
        default=[],
        metavar="TESTS",
        help="select a set of tests to run first. This can be used in situations"
        " where you want to run all tests, but care more about some set, "
        "e.g. after making a change to a specific component",
    )
    parser.add_argument(
        "--ignore-win-blocklist",
        action="store_true",
        help="always run blocklisted windows tests",
    )
    # NS: Disable target determination until it can be made more reliable
    # parser.add_argument(
    #     "--determine-from",
    #     help="File of affected source filenames to determine which tests to run.",
    # )
    parser.add_argument(
        "--continue-through-error",
        action="store_true",
        help="Runs the full test suite despite one of the tests failing",
        default=strtobool(os.environ.get("CONTINUE_THROUGH_ERROR", "False")),
    )
    parser.add_argument(
        "additional_unittest_args",
        nargs="*",
        help="additional arguments passed through to unittest, e.g., "
        "python run_test.py -i sparse -- TestSparse.test_factory_size_check",
    )
    parser.add_argument(
        "--export-past-test-times",
        nargs="?",
        type=str,
        const=TEST_TIMES_FILE,
        help="dumps test times from previous S3 stats into a file, format JSON",
    )
    parser.add_argument(
        "--shard",
        nargs=2,
        type=int,
        help="runs a shard of the tests (taking into account other selections), e.g., "
        "--shard 2 3 will break up the selected tests into 3 shards and run the tests "
        "in the 2nd shard (the first number should not exceed the second)",
    )
    parser.add_argument(
        "--exclude-jit-executor",
        action="store_true",
        help="exclude tests that are run for a specific jit config",
    )
    parser.add_argument(
        "--exclude-distributed-tests",
        action="store_true",
        help="exclude distributed tests",
    )
    parser.add_argument(
        "--run-specified-test-cases",
        nargs="?",
        type=str,
        const=SPECIFIED_TEST_CASES_FILE,
        help="load specified test cases file dumped from previous OSS CI stats, format CSV. "
        " If all test cases should run for a <test_module> please add a single row: \n"
        " test_filename,test_case_name\n"
        " ...\n"
        " <test_module>,__all__\n"
        " ...\n"
        'how we use the stats will be based on option "--use-specified-test-cases-by".',
    )
    parser.add_argument(
        "--use-specified-test-cases-by",
        type=str,
        choices=["include", "bring-to-front"],
        default="include",
        help='used together with option "--run-specified-test-cases". When specified test case '
        "file is set, this option allows the user to control whether to only run the specified test "
        "modules or to simply bring the specified modules to front and also run the remaining "
        "modules. Note: regardless of this option, we will only run the specified test cases "
        " within a specified test module. For unspecified test modules with the bring-to-front "
        "option, all test cases will be run, as one may expect.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list the test that will run.",
    )
    return parser.parse_args()


def find_test_index(test, selected_tests, find_last_index=False):
    """Find the index of the first or last occurrence of a given test/test module in the list of selected tests.

    This function is used to determine the indices when slicing the list of selected tests when
    ``options.first``(:attr:`find_last_index`=False) and/or ``options.last``(:attr:`find_last_index`=True) are used.

    :attr:`selected_tests` can be a list that contains multiple consequent occurrences of tests
    as part of the same test module, e.g.:

    ```
    selected_tests = ['autograd', 'cuda', **'torch.TestTorch.test_acos',
                     'torch.TestTorch.test_tan', 'torch.TestTorch.test_add'**, 'utils']
    ```

    If :attr:`test`='torch' and :attr:`find_last_index`=False, result should be **2**.
    If :attr:`test`='torch' and :attr:`find_last_index`=True, result should be **4**.

    Args:
        test (str): Name of test to lookup
        selected_tests (list): List of tests
        find_last_index (bool, optional): should we lookup the index of first or last
            occurrence (first is default)

    Returns:
        index of the first or last occurrence of the given test
    """
    idx = 0
    found_idx = -1
    for t in selected_tests:
        if t.startswith(test):
            found_idx = idx
            if not find_last_index:
                break
        idx += 1
    return found_idx


def exclude_tests(exclude_list, selected_tests, exclude_message=None):
    for exclude_test in exclude_list:
        tests_copy = selected_tests[:]
        for test in tests_copy:
            if test.startswith(exclude_test):
                if exclude_message is not None:
                    print_to_stderr("Excluding {} {}".format(test, exclude_message))
                selected_tests.remove(test)
    return selected_tests


def get_selected_tests(options):
    # First make sure run specific test cases options are processed.
    if options.run_specified_test_cases:
        if options.use_specified_test_cases_by == "include":
            options.include = list(SPECIFIED_TEST_CASES_DICT.keys())
        elif options.use_specified_test_cases_by == "bring-to-front":
            options.bring_to_front = list(SPECIFIED_TEST_CASES_DICT.keys())

    selected_tests = options.include

    # filter if there's JIT only and distributed only test options
    if options.jit:
        selected_tests = list(
            filter(lambda test_name: "jit" in test_name, selected_tests)
        )

    if options.distributed_tests:
        selected_tests = list(
            filter(lambda test_name: test_name in DISTRIBUTED_TESTS, selected_tests)
        )

    # Filter to only run core tests when --core option is specified
    if options.core:
        selected_tests = list(
            filter(lambda test_name: test_name in CORE_TEST_LIST, selected_tests)
        )

    # process reordering
    if options.bring_to_front:
        to_front = set(options.bring_to_front)
        selected_tests = options.bring_to_front + list(
            filter(lambda name: name not in to_front, selected_tests)
        )

    if options.first:
        first_index = find_test_index(options.first, selected_tests)
        selected_tests = selected_tests[first_index:]

    if options.last:
        last_index = find_test_index(options.last, selected_tests, find_last_index=True)
        selected_tests = selected_tests[: last_index + 1]

    # process exclusion
    if options.exclude_jit_executor:
        options.exclude.extend(JIT_EXECUTOR_TESTS)

    if options.exclude_distributed_tests:
        options.exclude.extend(DISTRIBUTED_TESTS)

    # these tests failing in CUDA 11.6 temporary disabling. issue https://github.com/pytorch/pytorch/issues/75375
    if torch.version.cuda is not None and LooseVersion(torch.version.cuda) == "11.6":
        options.exclude.extend(["distributions/test_constraints"])

    selected_tests = exclude_tests(options.exclude, selected_tests)

    if sys.platform == "win32" and not options.ignore_win_blocklist:
        target_arch = os.environ.get("VSCMD_ARG_TGT_ARCH")
        if target_arch != "x64":
            WINDOWS_BLOCKLIST.append("cpp_extensions_aot_no_ninja")
            WINDOWS_BLOCKLIST.append("cpp_extensions_aot_ninja")
            WINDOWS_BLOCKLIST.append("cpp_extensions_jit")
            WINDOWS_BLOCKLIST.append("jit")
            WINDOWS_BLOCKLIST.append("jit_fuser")

        # This is exception that's caused by this issue https://github.com/pytorch/pytorch/issues/69460
        # This below code should be removed once this issue is solved
        if torch.version.cuda is not None and LooseVersion(torch.version.cuda) >= "11.5":
            WINDOWS_BLOCKLIST.append("test_cpp_extensions_aot")
            WINDOWS_BLOCKLIST.append("test_cpp_extensions_aot_ninja")
            WINDOWS_BLOCKLIST.append("test_cpp_extensions_aot_no_ninja")

        selected_tests = exclude_tests(WINDOWS_BLOCKLIST, selected_tests, "on Windows")

    elif TEST_WITH_ROCM:
        selected_tests = exclude_tests(ROCM_BLOCKLIST, selected_tests, "on ROCm")

    # sharding
    if options.shard:
        assert len(options.shard) == 2, "Unexpected shard format"
        assert min(options.shard) > 0, "Shards must be positive numbers"
        which_shard, num_shards = options.shard
        assert (
            which_shard <= num_shards
        ), "Selected shard must be less than or equal to total number of shards"
        assert num_shards <= len(
            selected_tests
        ), f"Number of shards must be less than {len(selected_tests)}"
        # TODO: fix this to use test_times_filename, but currently this is not working
        # because setting the export arg immeidately halts the test execution.
        selected_tests = get_shard_based_on_S3(
            which_shard, num_shards, selected_tests, TEST_TIMES_FILE
        )

    # skip all distributed tests if distributed package is not available.
    if not dist.is_available():
        selected_tests = exclude_tests(DISTRIBUTED_TESTS, selected_tests,
                                       "PyTorch is built without distributed support.")

    # skip tests that require LAPACK when it's not available
    if not torch._C.has_lapack:
        selected_tests = exclude_tests(TESTS_REQUIRING_LAPACK, selected_tests,
                                       "PyTorch is built without LAPACK support.")

    return selected_tests


def run_test_module(test: str, test_directory: str, options) -> Optional[str]:
    test_module = parse_test_module(test)

    # Printing the date here can help diagnose which tests are slow
    print_to_stderr("Running {} ... [{}]".format(test, datetime.now()))
    handler = CUSTOM_HANDLERS.get(test_module, run_test)
    return_code = handler(test_module, test_directory, options)
    assert isinstance(return_code, int) and not isinstance(
        return_code, bool
    ), "Return code should be an integer"
    if return_code == 0:
        return None

    message = f"{test} failed!"
    if return_code < 0:
        # subprocess.Popen returns the child process' exit signal as
        # return code -N, where N is the signal number.
        signal_name = SIGNALS_TO_NAMES_DICT[-return_code]
        message += f" Received signal: {signal_name}"
    return message


def main():
    options = parse_args()

    # TODO: move this export & download function in tools/ folder
    test_times_filename = options.export_past_test_times
    if test_times_filename:
        print(
            f"Exporting past test times from S3 to {test_times_filename}, no tests will be run."
        )
        export_S3_test_times(test_times_filename)
        return

    specified_test_cases_filename = options.run_specified_test_cases
    if specified_test_cases_filename:
        print(
            f"Loading specified test cases to run from {specified_test_cases_filename}."
        )
        global SPECIFIED_TEST_CASES_DICT
        SPECIFIED_TEST_CASES_DICT = get_specified_test_cases(
            specified_test_cases_filename, TESTS
        )

    test_directory = str(REPO_ROOT / "test")
    selected_tests = get_selected_tests(options)

    if options.verbose:
        print_to_stderr("Selected tests:\n {}".format("\n ".join(selected_tests)))

    if options.dry_run:
        return

    if options.coverage and not PYTORCH_COLLECT_COVERAGE:
        shell(["coverage", "erase"])

    # NS: Disable target determination until it can be made more reliable
    # if options.determine_from is not None and os.path.exists(options.determine_from):
    #     slow_tests = get_slow_tests_based_on_S3(
    #         TESTS, TARGET_DET_LIST, SLOW_TEST_THRESHOLD
    #     )
    #     print_to_stderr(
    #         "Added the following tests to target_det tests as calculated based on S3:"
    #     )
    #     print_to_stderr(slow_tests)
    #     with open(options.determine_from, "r") as fh:
    #         touched_files = [
    #             os.path.normpath(name.strip())
    #             for name in fh.read().split("\n")
    #             if len(name.strip()) > 0
    #         ]
    #     # HACK: Ensure the 'test' paths can be traversed by Modulefinder
    #     sys.path.append(test_directory)
    #     selected_tests = [
    #         test
    #         for test in selected_tests
    #         if should_run_test(
    #             TARGET_DET_LIST + slow_tests, test, touched_files, options
    #         )
    #     ]
    #     sys.path.remove(test_directory)

    if IS_IN_CI:
        selected_tests = get_reordered_tests(
            selected_tests, ENABLE_PR_HISTORY_REORDERING
        )
        # downloading test cases configuration to local environment
        get_test_case_configs(dirpath=test_directory)

    has_failed = False
    failure_messages = []
    try:
        for test in selected_tests:
            options_clone = copy.deepcopy(options)
            if test in USE_PYTEST_LIST:
                options_clone.pytest = True
            err_message = run_test_module(test, test_directory, options_clone)
            if err_message is None:
                continue
            has_failed = True
            failure_messages.append(err_message)
            if not options_clone.continue_through_error:
                raise RuntimeError(err_message)
            print_to_stderr(err_message)
    finally:
        if options.coverage:
            from coverage import Coverage

            with set_cwd(test_directory):
                cov = Coverage()
                if PYTORCH_COLLECT_COVERAGE:
                    cov.load()
                cov.combine(strict=False)
                cov.save()
                if not PYTORCH_COLLECT_COVERAGE:
                    cov.html_report()

    if options.continue_through_error and has_failed:
        for err in failure_messages:
            print_to_stderr(err)
        sys.exit(1)


if __name__ == "__main__":
    main()
