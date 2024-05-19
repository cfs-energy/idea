#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

"""
DynamoDB Local Server for Unit Tests
"""

from ideasdk.shell import ShellInvoker, StreamInvocationProcess
from ideasdk.utils import Utils, EnvironmentUtils
from ideatestutils import IdeaTestProps

import os
import requests
import shutil
import pathlib
from threading import Thread
from typing import Optional

DYNAMODB_LOCAL_PACKAGE_URL = 'https://s3.us-west-2.amazonaws.com/dynamodb-local/dynamodb_local_latest.zip'
DYNAMODB_LOCAL_PACKAGE_SHA256_CHECKSUM = '7ec2f8d538f4b026dacecc944ef68dc5a39878b702c866365f286c8e349d81e1'


class DynamoDBLocal:
    """
    Manage DynamoDBLocal server for unit tests.
    * handles ddb local installation
    * exposes start and stop methods to manage server life-cycle based on unit test scope
    """

    def __init__(self, db_name: str = None, port: int = 9000, reset: bool = False, cleanup: bool = False):
        """
        :param db_dir: the directory where the database will be initialized. defaults to ~/.idea/tests/ddb
        :param port: the port on which ddb local can be accessible. ddb endpoint url will be: http://localhost:9000
        :param reset: the ddb_dir will be deleted prior to starting
        :param cleanup: the ddb_dir will be deleted after stopping
        """
        self.db_name = db_name
        self.port = port
        self.reset = reset
        self.cleanup = cleanup
        self.shell = ShellInvoker()
        self.props = IdeaTestProps()

        if Utils.is_empty(self.db_name):
            self.db_name = 'default'

        base_db_dir = self.props.get_test_dir('ddb')
        self.db_dir = os.path.join(base_db_dir, self.db_name)

        self.process: Optional[StreamInvocationProcess] = None
        self.loop = Thread(
            target=self._run_ddb_local,
            name='ddb-local'
        )

    def get_ddb_local_download_dir(self) -> str:
        idea_user_home_dir = self.props.get_idea_user_home_dir()
        downloads_dir = os.path.join(idea_user_home_dir, 'downloads')
        os.makedirs(downloads_dir, exist_ok=True)
        return downloads_dir

    def get_ddb_local_zip_file(self) -> str:
        return os.path.join(self.get_ddb_local_download_dir(), 'dynamodb-local.zip')

    def get_ddb_local_lib_dir(self) -> str:
        idea_user_home_dir = self.props.get_idea_user_home_dir()
        lib_dir = os.path.join(idea_user_home_dir, 'lib')
        return os.path.join(lib_dir, 'dynamodb-local')

    def get_ddb_local_jar_file(self) -> str:
        ddb_local_lib_dir = self.get_ddb_local_lib_dir()
        return os.path.join(ddb_local_lib_dir, 'DynamoDBLocal.jar')

    def get_ddb_local_java_libs_dir(self) -> str:
        ddb_local_lib_dir = self.get_ddb_local_lib_dir()
        return os.path.join(ddb_local_lib_dir, 'DynamoDBLocal_lib')

    def check_and_install(self) -> bool:
        """
        download and install ddb local in ~/.idea/lib/dynamodb-local
        """

        # check pre-requisites
        which_java = self.shell.invoke(['command', '-v', 'java'])
        if which_java.returncode != 0:
            print('dynamodb local cannot be initialized. Java Runtime Environment (JRE) is required.')
            return False

        # download and install (if not already installed)
        ddb_local_zip_file = self.get_ddb_local_zip_file()
        if not Utils.is_file(ddb_local_zip_file):
            print(f'downloading ddb local: {DYNAMODB_LOCAL_PACKAGE_URL} ...')
            request = requests.get(DYNAMODB_LOCAL_PACKAGE_URL)
            with open(ddb_local_zip_file, 'wb') as f:
                f.write(request.content)
            sha256_checksum = Utils.compute_checksum_for_file(ddb_local_zip_file)
            if sha256_checksum != DYNAMODB_LOCAL_PACKAGE_SHA256_CHECKSUM:
                print('failed to verify checksum. skip.')
                return False

        ddb_local_jar_file = self.get_ddb_local_jar_file()
        if Utils.is_file(ddb_local_jar_file):
            return True

        # directory exists but zip does not exist. clean up
        ddb_local_lib_dir = self.get_ddb_local_lib_dir()
        if Utils.is_dir(ddb_local_lib_dir):
            shutil.rmtree(ddb_local_lib_dir)

        os.makedirs(str(pathlib.Path(ddb_local_lib_dir).parent), exist_ok=True)

        print('installing ddb local ...')
        shutil.unpack_archive(ddb_local_zip_file, self.get_ddb_local_lib_dir(), 'zip')

    def _run_ddb_local(self):
        cmd = [
            'java',
            f'-Djava.library.path={self.get_ddb_local_java_libs_dir()}',
            '-jar',
            self.get_ddb_local_jar_file(),
            '-port',
            f'{self.port}',
            '-dbPath',
            self.db_dir,
            '-sharedDb'
        ]
        print(' '.join(cmd))
        self.process = self.shell.invoke_stream(
            cmd=cmd,
            callback=lambda line: print(line, end='')
        )

        print(f'dynamodb local started. endpoint url: http://localhost:{self.port}')

        try:
            self.process.start_streaming()
        except KeyboardInterrupt:
            self.process.send_stop_signal()
            self.process.wait()

        print('ddb local stopped.')

    def start(self):
        if self.reset and Utils.is_dir(self.db_dir):
            print(f'reset db dir: {self.db_dir}')
            shutil.rmtree(self.db_dir)

        os.makedirs(self.db_dir, exist_ok=True)

        print('starting ddb local ...')
        self.loop.start()

    def stop(self):
        print('stopping ddb local ...')
        if self.process is not None:
            self.process.send_stop_signal()

        self.loop.join()

        if self.cleanup and Utils.is_dir(self.db_dir):
            print(f'clean-up db dir: {self.db_dir}')
            shutil.rmtree(self.db_dir)


if __name__ == '__main__':
    db = DynamoDBLocal(
        db_name='cluster-manager',
        reset=True
    )

    import time

    db.start()
    time.sleep(10)
    db.stop()
