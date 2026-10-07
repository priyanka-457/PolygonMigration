import hashlib
import time
import random
import string
import requests
from urllib.parse import urlencode
from django.conf import settings
import json
import os
import sys
import zipfile
import tempfile
import redis
import logging
import shutil
import subprocess
import stat

from .storage import get_storage_provider

logger = logging.getLogger(__name__)


class PolygonAPI:
    """
    Service class for interacting with the Polygon API.

    Handles:
    - Polygon API authentication
    - Problem information
    - Statements
    - Test cases
    - Custom checkers
    - Redis caching
    - Provider-independent storage migration
    """

    API_URL = "https://polygon.codeforces.com/api/"

    def __init__(self):
        """
        Initialize PolygonAPI with credentials from Django settings.
        """
        self.api_key = settings.POLYGON_API_KEY
        self.api_secret = settings.POLYGON_API_SECRET

    # ------------------------------------------------------------------
    # POLYGON API AUTHENTICATION
    # ------------------------------------------------------------------

    def _generate_api_sig(self, method_name, params):
        """
        Generate the required Polygon apiSig.
        """
        params["apiKey"] = self.api_key
        params["time"] = int(time.time())

        sorted_params = sorted(params.items())
        param_str = urlencode(sorted_params)

        rand_prefix = "".join(
            random.choices(
                string.ascii_lowercase + string.digits,
                k=6
            )
        )

        to_hash = (
            f"{rand_prefix}/{method_name}?{param_str}#{self.api_secret}"
        )

        sha512_hash = hashlib.sha512(
            to_hash.encode("utf-8")
        ).hexdigest()

        api_sig = f"{rand_prefix}{sha512_hash}"

        return api_sig, params["time"]

    def _make_request(
        self,
        method_name,
        params=None,
        expect_json=True
    ):
        """
        Make a POST request to the Polygon API.
        """
        if params is None:
            params = {}

        api_sig, request_time = self._generate_api_sig(
            method_name,
            params.copy()
        )

        post_params = params.copy()
        post_params["apiKey"] = self.api_key
        post_params["apiSig"] = api_sig
        post_params["time"] = request_time

        try:
            response = requests.post(
                f"{self.API_URL}{method_name}",
                data=post_params
            )

            response.raise_for_status()

            if not expect_json:
                return response.text

            try:
                data = response.json()

                if data.get("status") == "FAILED":
                    raise Exception(
                        f"Polygon API Error: {data.get('comment')}"
                    )

                return data.get("result")

            except json.JSONDecodeError:
                if expect_json:
                    raise

                return response.text

        except requests.exceptions.RequestException as e:
            raise Exception(
                f"HTTP Request Error: {e}"
            )

    def _make_plain_request(
        self,
        method_name,
        params=None
    ):
        """
        Make a Polygon API request expecting plain text.
        """
        return self._make_request(
            method_name,
            params,
            expect_json=False
        )

    # ------------------------------------------------------------------
    # PACKAGE
    # ------------------------------------------------------------------

    def download_and_extract_package(
        self,
        problem_id,
        package_type="standard"
    ):
        """
        Download the problem package from Polygon and extract problem.html.
        """
        logger.info(
            "Downloading package for problem %s",
            problem_id
        )

        temp_dir = tempfile.mkdtemp()

        try:
            problem_info = self.get_problem_info(problem_id)

            if not problem_info:
                raise Exception(
                    "Could not get problem info"
                )

            packages = self._make_request(
                "problem.packages",
                {
                    "problemId": problem_id
                }
            )

            if not packages:
                raise Exception(
                    "No packages available for this problem"
                )

            latest_package = None

            for package in packages:
                if package.get("type") == package_type:
                    if (
                        not latest_package
                        or package.get("revision", 0)
                        > latest_package.get("revision", 0)
                    ):
                        latest_package = package

            if not latest_package:
                raise Exception(
                    f"No {package_type} package found for this problem"
                )

            package_id = latest_package["id"]

            api_sig, request_time = self._generate_api_sig(
                "problem.package",
                {
                    "problemId": problem_id,
                    "packageId": package_id,
                    "type": package_type
                }
            )

            post_params = {
                "problemId": problem_id,
                "packageId": package_id,
                "type": package_type,
                "apiKey": self.api_key,
                "apiSig": api_sig,
                "time": request_time
            }

            response = requests.post(
                f"{self.API_URL}problem.package",
                data=post_params
            )

            response.raise_for_status()

            if not response.content.startswith(b"PK"):
                try:
                    error_data = response.json()

                    if error_data.get("status") == "FAILED":
                        raise Exception(
                            "Polygon API Error: "
                            f"{error_data.get('comment')}"
                        )
                except Exception:
                    pass

                raise Exception(
                    "Downloaded data is not a valid ZIP file"
                )

            zip_path = os.path.join(
                temp_dir,
                f"problem_{problem_id}.zip"
            )

            with open(zip_path, "wb") as f:
                f.write(response.content)

            extract_dir = os.path.join(
                temp_dir,
                "extracted"
            )

            os.makedirs(
                extract_dir,
                exist_ok=True
            )

            with zipfile.ZipFile(
                zip_path,
                "r"
            ) as zip_ref:
                zip_ref.extractall(extract_dir)

            problem_html_path = None

            for root, dirs, files in os.walk(extract_dir):
                for file in files:
                    if file == "problem.html":
                        problem_html_path = os.path.join(
                            root,
                            file
                        )
                        break

                if problem_html_path:
                    break

            if not problem_html_path:
                raise Exception(
                    "problem.html not found in the extracted package"
                )

            with open(
                problem_html_path,
                "r",
                encoding="utf-8"
            ) as f:
                problem_html_content = f.read()

            logger.info(
                "Successfully read problem.html content, length: %d characters",
                len(problem_html_content)
            )

            return problem_html_content

        except Exception as e:
            raise Exception(
                f"Error downloading/extracting package: {e}"
            )

        finally:
            try:
                shutil.rmtree(temp_dir)

                logger.debug(
                    "Cleaned up temporary directory: %s",
                    temp_dir
                )

            except Exception as cleanup_error:
                logger.warning(
                    "Failed to clean up temporary directory %s: %s",
                    temp_dir,
                    cleanup_error
                )

    # ------------------------------------------------------------------
    # PROBLEM INFORMATION
    # ------------------------------------------------------------------

    def get_problem_info(self, problem_id):
        """
        Fetch problem information from Polygon.
        """
        logger.info(
            "Fetching problem info for %s",
            problem_id
        )

        info = self._make_request(
            "problem.info",
            {
                "problemId": problem_id
            }
        )

        logger.info(
            "Polygon problem.info response: %s",
            info
        )

        return info

    def get_statements(self, problem_id):
        """
        Fetch problem statements from Polygon.
        """
        logger.info(
            "Fetching statements for %s",
            problem_id
        )

        return self._make_request(
            "problem.statements",
            {
                "problemId": problem_id
            }
        )

    def get_test_script(
        self,
        problem_id,
        testset="tests"
    ):
        """
        Fetch the test script for a problem.
        """
        logger.info(
            "Fetching test script for %s, testset %s",
            problem_id,
            testset
        )

        try:
            return self._make_request(
                "problem.script",
                {
                    "problemId": problem_id,
                    "testset": testset
                }
            )

        except Exception:
            return (
                "Test script not found or could not be fetched."
            )

    def get_test_cases(
        self,
        problem_id,
        testset="tests"
    ):
        """
        Fetch all test cases from Polygon.
        """
        logger.info(
            "Fetching test cases for %s, testset %s",
            problem_id,
            testset
        )

        try:
            return self._make_request(
                "problem.tests",
                {
                    "problemId": problem_id,
                    "testset": testset
                }
            )

        except Exception:
            return []

    def get_problem_files(self, problem_id):
        """
        Fetch all files associated with a problem.
        """
        logger.info(
            "Fetching problem files for %s",
            problem_id
        )

        try:
            return self._make_request(
                "problem.files",
                {
                    "problemId": problem_id
                }
            )

        except Exception:
            return {}

    def get_file_content(
        self,
        problem_id,
        file_type,
        file_name
    ):
        """
        Fetch content of a specific Polygon file.
        """
        logger.info(
            "Fetching content of %s file %s for problem %s",
            file_type,
            file_name,
            problem_id
        )

        try:
            return self._make_request(
                "problem.viewFile",
                {
                    "problemId": problem_id,
                    "type": file_type,
                    "name": file_name
                }
            )

        except Exception:
            return (
                f"File {file_name} not found or could not be fetched."
            )

    # ------------------------------------------------------------------
    # TEST CASES
    # ------------------------------------------------------------------

    def get_all_test_cases(
        self,
        problem_id,
        testset="tests"
    ):
        """
        Fetch all test cases from Polygon.
        """
        logger.info(
            "Fetching all test cases for %s, testset %s",
            problem_id,
            testset
        )

        tests = self._make_request(
            "problem.tests",
            {
                "problemId": problem_id,
                "testset": testset
            }
        )

        all_cases = []

        logger.info(
            "Found %d test cases to process",
            len(tests)
        )

        for test in tests:
            test_index = test["index"]

            logger.info(
                "Processing test case %d/%d (index: %s)",
                len(all_cases) + 1,
                len(tests),
                test_index
            )

            test_case = {
                "index": test_index,
                "manual": test.get(
                    "manual",
                    False
                ),
                "is_sample": test.get(
                    "useInStatements",
                    False
                ),
                "description": test.get(
                    "description",
                    ""
                )
            }

            try:
                logger.debug(
                    "Fetching input for test case %s",
                    test_index
                )

                test_case["input"] = (
                    self._make_plain_request(
                        "problem.testInput",
                        {
                            "problemId": problem_id,
                            "testset": testset,
                            "testIndex": test_index
                        }
                    )
                    or ""
                )

                logger.debug(
                    "Fetching output for test case %s",
                    test_index
                )

                test_case["output"] = (
                    self._make_plain_request(
                        "problem.testAnswer",
                        {
                            "problemId": problem_id,
                            "testset": testset,
                            "testIndex": test_index
                        }
                    )
                    or ""
                )

                logger.info(
                    "Successfully fetched test case %s - "
                    "Input length: %d, Output length: %d",
                    test_index,
                    len(test_case["input"]),
                    len(test_case["output"])
                )

            except Exception as e:
                logger.warning(
                    "Error fetching test case %s: %s",
                    test_index,
                    e
                )

                test_case["input"] = ""
                test_case["output"] = ""

            all_cases.append(test_case)

        logger.info(
            "Completed fetching all %d test cases for problem %s",
            len(all_cases),
            problem_id
        )

        return all_cases

    # ------------------------------------------------------------------
    # CUSTOM CHECKER
    # ------------------------------------------------------------------

    def get_custom_checker_info(self, problem_id):
        """
        Get information about the custom checker.
        """
        logger.info(
            "Fetching custom checker info for problem %s",
            problem_id
        )

        try:
            checker_info = self._make_request(
                "problem.checker",
                {
                    "problemId": problem_id
                }
            )

            logger.debug(
                "Raw checker info: %s",
                checker_info
            )

            if checker_info and not checker_info.startswith("std::"):
                result = {
                    "name": checker_info,
                    "type": "custom"
                }

                logger.info(
                    "Custom checker detected: %s",
                    result
                )

                return result

            logger.info(
                "No custom checker found, using standard checker: %s",
                checker_info
            )

            return None

        except Exception as e:
            logger.error(
                "Error fetching custom checker info: %s",
                e
            )

            return None

    def fetch_custom_checker_file(
        self,
        problem_id,
        checker_name
    ):
        """
        Fetch custom checker source code from Polygon.
        """
        logger.info(
            "Fetching custom checker file %s via API for problem %s",
            checker_name,
            problem_id
        )

        try:
            content = self._make_plain_request(
                "problem.viewFile",
                {
                    "problemId": problem_id,
                    "type": "source",
                    "name": checker_name
                }
            )

            logger.info(
                "Successfully fetched custom checker file via API, "
                "content length: %d",
                len(content) if content else 0
            )

            return content

        except Exception as e:
            logger.error(
                "Error fetching custom checker file as source: %s",
                e
            )

            try:
                content = self._make_plain_request(
                    "problem.viewFile",
                    {
                        "problemId": problem_id,
                        "type": "resource",
                        "name": checker_name
                    }
                )

                logger.info(
                    "Successfully fetched custom checker file as resource, "
                    "content length: %d",
                    len(content) if content else 0
                )

                return content

            except Exception as e2:
                logger.error(
                    "Error fetching custom checker file as resource: %s",
                    e2
                )

                if not checker_name.endswith(".cpp"):
                    try:
                        content = self._make_plain_request(
                            "problem.viewFile",
                            {
                                "problemId": problem_id,
                                "type": "source",
                                "name": checker_name + ".cpp"
                            }
                        )

                        logger.info(
                            "Successfully fetched custom checker file "
                            "with .cpp extension, content length: %d",
                            len(content) if content else 0
                        )

                        return content

                    except Exception as e3:
                        logger.error(
                            "Error fetching custom checker file "
                            "with .cpp extension: %s",
                            e3
                        )

                return None

    def compile_custom_checker(
        self,
        source_code,
        temp_dir
    ):
        """
        Compile custom checker source code using g++.
        """
        checker_dir = settings.CUSTOM_CHECKER_DIR

        if checker_dir:
            if not os.path.exists(checker_dir):
                os.makedirs(
                    checker_dir,
                    exist_ok=True
                )

            temp_dir = checker_dir

        logger.info(
            "Compiling custom checker in %s",
            temp_dir
        )

        gpp_path = shutil.which("g++")

        if not gpp_path:
            logger.error(
                "g++ compiler not found in system PATH."
            )

            return None

        logger.info(
            "Found g++ at: %s",
            gpp_path
        )

        source_file = os.path.join(
            temp_dir,
            "custom_checker.cpp"
        )

        with open(
            source_file,
            "w",
            encoding="utf-8"
        ) as f:
            f.write(source_code)

        logger.info(
            "Source code written to: %s",
            source_file
        )

        if sys.platform.startswith("win"):
            binary_filename = "custom_checker.exe"
        else:
            binary_filename = "custom_checker"

        binary_file = os.path.join(
            temp_dir,
            binary_filename
        )

        try:
            logger.info(
                "Running compilation command: "
                "g++ -std=gnu++17 -O2 -o %s %s",
                binary_file,
                source_file
            )

            result = subprocess.run(
                [
                    gpp_path,
                    "-std=gnu++17",
                    "-O2",
                    "-o",
                    binary_file,
                    source_file
                ],
                capture_output=True,
                text=True,
                cwd=temp_dir,
                timeout=30
            )

            logger.info(
                "Compilation completed with return code: %d",
                result.returncode
            )

            if result.stdout:
                logger.debug(
                    "Compilation stdout: %s",
                    result.stdout
                )

            if result.stderr:
                logger.debug(
                    "Compilation stderr: %s",
                    result.stderr
                )

            if result.returncode == 0:
                if os.path.exists(binary_file):
                    os.chmod(
                        binary_file,
                        stat.S_IRWXU
                        | stat.S_IRGRP
                        | stat.S_IXGRP
                    )

                    logger.info(
                        "Custom checker compiled successfully"
                    )

                    return binary_file

                logger.error(
                    "Compilation succeeded but binary file "
                    "was not created"
                )

                return None

            logger.error(
                "Compilation failed with return code %d",
                result.returncode
            )

            return None

        except subprocess.TimeoutExpired:
            logger.error(
                "Compilation timed out"
            )

            return None

        except FileNotFoundError as e:
            logger.error(
                "g++ compiler not found: %s",
                e
            )

            return None

        except Exception as e:
            logger.error(
                "Error during compilation: %s",
                e
            )

            return None

    # ------------------------------------------------------------------
    # CUSTOM CHECKER STORAGE
    # ------------------------------------------------------------------

    def upload_custom_checker_to_azure(
        self,
        problem_id,
        azure_account_url,
        azure_tenant_id,
        azure_client_id,
        azure_client_secret,
        container_name,
        db_problem_id=None
    ):
        """
        Fetch, compile and upload the custom checker.

        The method name is retained for backward compatibility with
        the existing view layer. Internally, storage is provider-independent.
        """
        logger.info(
            "Processing custom checker for problem %s",
            problem_id
        )

        checker_info = self.get_custom_checker_info(
            problem_id
        )

        if not checker_info:
            logger.info(
                "No custom checker found for problem %s",
                problem_id
            )
            return

        logger.info(
            "Custom checker info retrieved: %s",
            checker_info
        )

        source_code = self.fetch_custom_checker_file(
            problem_id,
            checker_info["name"]
        )

        if not source_code:
            logger.error(
                "Failed to fetch custom checker source code"
            )
            return

        logger.info(
            "Custom checker source code fetched successfully, "
            "length: %d",
            len(source_code)
        )

        storage = get_storage_provider()

        checker_dir = settings.CUSTOM_CHECKER_DIR

        if checker_dir:
            if not os.path.exists(checker_dir):
                os.makedirs(
                    checker_dir,
                    exist_ok=True
                )

            temp_dir = checker_dir

            binary_path = self.compile_custom_checker(
                source_code,
                temp_dir
            )

            if not binary_path:
                logger.warning(
                    "Failed to compile custom checker, "
                    "uploading source code instead"
                )

                problem_id_for_naming = (
                    db_problem_id
                    if db_problem_id is not None
                    else problem_id
                )

                blob_name = (
                    f"test_cases/"
                    f"{problem_id_for_naming}/"
                    f"custom_checker.cpp"
                )

                storage.upload_bytes(
                    container_name,
                    blob_name,
                    source_code.encode("utf-8")
                )

                logger.info(
                    "Uploaded custom checker source code to %s",
                    blob_name
                )

                return

            binary_path_to_upload = binary_path

        else:
            temp_dir = tempfile.mkdtemp()

            try:
                binary_path_to_upload = (
                    self.compile_custom_checker(
                        source_code,
                        temp_dir
                    )
                )

                if not binary_path_to_upload:
                    logger.warning(
                        "Failed to compile custom checker, "
                        "uploading source code instead"
                    )

                    problem_id_for_naming = (
                        db_problem_id
                        if db_problem_id is not None
                        else problem_id
                    )

                    blob_name = (
                        f"test_cases/"
                        f"{problem_id_for_naming}/"
                        f"custom_checker.cpp"
                    )

                    storage.upload_bytes(
                        container_name,
                        blob_name,
                        source_code.encode("utf-8")
                    )

                    logger.info(
                        "Uploaded custom checker source code to %s",
                        blob_name
                    )

                    return

            finally:
                if os.path.exists(temp_dir):
                    shutil.rmtree(
                        temp_dir,
                        ignore_errors=True
                    )

        try:
            with open(
                binary_path_to_upload,
                "rb"
            ) as f:
                binary_data = f.read()

            logger.info(
                "Binary file read successfully, size: %d bytes",
                len(binary_data)
            )

        except Exception as e:
            logger.error(
                "Error reading compiled binary: %s",
                e
            )
            return

        problem_id_for_naming = (
            db_problem_id
            if db_problem_id is not None
            else problem_id
        )

        if sys.platform.startswith("win"):
            blob_filename = "custom_checker.exe"
        else:
            blob_filename = "custom_checker"

        blob_name = (
            f"test_cases/"
            f"{problem_id_for_naming}/"
            f"{blob_filename}"
        )

        try:
            storage.upload_bytes(
                container_name,
                blob_name,
                binary_data
            )

            logger.info(
                "Successfully uploaded custom checker to %s",
                blob_name
            )

        except Exception as e:
            logger.error(
                "Error uploading custom checker: %s",
                e
            )
            raise

    # ------------------------------------------------------------------
    # TEST CASE STORAGE MIGRATION
    # ------------------------------------------------------------------

    def migrate_to_azure_blob(
        self,
        problem_id,
        azure_account_url,
        azure_tenant_id,
        azure_client_id,
        azure_client_secret,
        container_name,
        db_problem_id=None,
        testset="tests"
    ):
        """
        Fetch all test cases from Redis or Polygon and migrate them
        through the configured storage provider.

        The method name is retained for backward compatibility.
        """

        logger.info(
            "Starting storage migration for problem %s",
            problem_id
        )

        test_cases = self.get_test_cases_from_redis(
            problem_id
        )

        if test_cases is None:
            logger.warning(
                "Test cases not found in Redis, fetching from Polygon"
            )

            test_cases = self.get_all_test_cases(
                problem_id,
                testset
            )

            self.store_test_cases_in_redis(
                problem_id,
                test_cases,
                expiry_hours=0.5
            )

        else:
            logger.info(
                "Retrieved test cases from Redis for storage migration"
            )

        logger.info(
            "Retrieved %d test cases for migration",
            len(test_cases)
        )

        storage = get_storage_provider()

        problem_id_for_naming = (
            db_problem_id
            if db_problem_id is not None
            else problem_id
        )

        logger.info(
            "Using problem_id_for_naming: %s",
            problem_id_for_naming
        )

        logger.info(
            "Deleting existing test cases from storage"
        )

        storage.empty_problem(
            container_name,
            problem_id_for_naming
        )

        uploaded_count = 0

        for idx, test in enumerate(
            test_cases,
            start=1
        ):
            input_data = test.get(
                "input",
                ""
            )

            output_data = test.get(
                "output",
                ""
            )

            if input_data and output_data:
                storage.upload_test_case(
                    container_name,
                    problem_id_for_naming,
                    idx,
                    input_data,
                    output_data
                )

                uploaded_count += 1

                logger.info(
                    "Uploaded test case #%d",
                    idx
                )

            else:
                logger.warning(
                    "Skipping test case #%d: missing input or output",
                    idx
                )

        logger.info(
            "Uploaded %d test cases to storage",
            uploaded_count
        )

        logger.info(
            "Starting custom checker processing for problem %s",
            problem_id
        )

        self.upload_custom_checker_to_azure(
            problem_id,
            azure_account_url,
            azure_tenant_id,
            azure_client_id,
            azure_client_secret,
            container_name,
            db_problem_id
        )

        logger.info(
            "Completed custom checker processing for problem %s",
            problem_id
        )

        logger.info(
            "Completed storage migration for problem %s",
            problem_id
        )

    # ------------------------------------------------------------------
    # REDIS CACHE
    # ------------------------------------------------------------------

    def delete_problem_test_case_cache(
        self,
        db_problem_id
    ):
        """
        Delete all Redis cache keys related to test cases.
        """
        pattern = (
            f"oj_dev_with_redis_storage_test_cases_"
            f"{db_problem_id}*"
        )

        logger.info(
            "pattern %s",
            pattern
        )

        try:
            redis_host = settings.REDIS_HOST
            redis_port = settings.REDIS_PORT
            redis_password = settings.REDIS_PASSWORD
            redis_ssl = settings.REDIS_SSL

            r = redis.StrictRedis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                ssl=redis_ssl,
                ssl_cert_reqs=None
            )

            for key in r.scan_iter(pattern):
                logger.info(
                    "deleting key %s",
                    key
                )

                r.delete(key)

        except Exception as e:
            logger.error(
                "Error deleting cache keys for pattern %s: %s",
                pattern,
                e
            )

    def store_test_cases_in_redis(
        self,
        polygon_id,
        test_cases,
        expiry_hours=0.5
    ):
        """
        Store test cases in Redis.
        """
        try:
            redis_host = settings.REDIS_HOST
            redis_port = settings.REDIS_PORT
            redis_password = settings.REDIS_PASSWORD
            redis_ssl = settings.REDIS_SSL

            r = redis.StrictRedis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                ssl=redis_ssl,
                ssl_cert_reqs=None
            )

            prefix = (
                f"polygon_migration_test_cases_{polygon_id}"
            )

            count_key = f"{prefix}_count"

            r.setex(
                count_key,
                int(expiry_hours * 3600),
                len(test_cases)
            )

            for idx, test_case in enumerate(
                test_cases,
                start=1
            ):
                test_case_key = (
                    f"{prefix}_test_{idx}"
                )

                test_case_data = {
                    "index": test_case.get(
                        "index",
                        idx
                    ),
                    "input": test_case.get(
                        "input",
                        ""
                    ),
                    "output": test_case.get(
                        "output",
                        ""
                    ),
                    "description": test_case.get(
                        "description",
                        ""
                    ),
                    "is_sample": test_case.get(
                        "is_sample",
                        False
                    )
                }

                r.setex(
                    test_case_key,
                    int(expiry_hours * 3600),
                    json.dumps(test_case_data)
                )

            logger.info(
                "Stored %d test cases in Redis for polygon_id %s",
                len(test_cases),
                polygon_id
            )

        except Exception as e:
            logger.error(
                "Error storing test cases in Redis for polygon_id %s: %s",
                polygon_id,
                e
            )

    def get_test_cases_from_redis(
        self,
        polygon_id
    ):
        """
        Retrieve test cases from Redis.
        """
        try:
            redis_host = settings.REDIS_HOST
            redis_port = settings.REDIS_PORT
            redis_password = settings.REDIS_PASSWORD
            redis_ssl = settings.REDIS_SSL

            r = redis.StrictRedis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                ssl=redis_ssl,
                ssl_cert_reqs=None
            )

            prefix = (
                f"polygon_migration_test_cases_{polygon_id}"
            )

            count_key = f"{prefix}_count"

            count = r.get(count_key)

            if count is None:
                logger.info(
                    "No test cases found in Redis for polygon_id %s",
                    polygon_id
                )

                return None

            count = int(count)

            test_cases = []

            for idx in range(
                1,
                count + 1
            ):
                test_case_key = (
                    f"{prefix}_test_{idx}"
                )

                test_case_data = r.get(
                    test_case_key
                )

                if test_case_data:
                    test_case = json.loads(
                        test_case_data
                    )

                    test_cases.append(
                        test_case
                    )

                else:
                    logger.warning(
                        "Missing test case %d in Redis for polygon_id %s",
                        idx,
                        polygon_id
                    )

            logger.info(
                "Retrieved %d test cases from Redis for polygon_id %s",
                len(test_cases),
                polygon_id
            )

            return test_cases

        except Exception as e:
            logger.error(
                "Error retrieving test cases from Redis "
                "for polygon_id %s: %s",
                polygon_id,
                e
            )

            return None

    def clear_test_cases_from_redis(
        self,
        polygon_id
    ):
        """
        Clear all Redis test case keys for a Polygon problem.
        """
        try:
            redis_host = settings.REDIS_HOST
            redis_port = settings.REDIS_PORT
            redis_password = settings.REDIS_PASSWORD
            redis_ssl = settings.REDIS_SSL

            r = redis.StrictRedis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                ssl=redis_ssl,
                ssl_cert_reqs=None
            )

            prefix = (
                f"polygon_migration_test_cases_{polygon_id}"
            )

            pattern = f"{prefix}*"

            deleted_count = 0

            for key in r.scan_iter(pattern):
                r.delete(key)
                deleted_count += 1

            logger.info(
                "Cleared %d test case keys from Redis "
                "for polygon_id %s",
                deleted_count,
                polygon_id
            )

        except Exception as e:
            logger.error(
                "Error clearing test cases from Redis "
                "for polygon_id %s: %s",
                polygon_id,
                e
            )