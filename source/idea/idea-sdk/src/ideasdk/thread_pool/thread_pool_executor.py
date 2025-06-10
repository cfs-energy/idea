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

__all__ = ('ThreadPoolMaxCapacity', 'ThreadPoolExecutor')

from typing import Set, List, Tuple
from threading import Thread, Event
import queue
from fastcounter import FastWriteCounter

from ideasdk.protocols import SocaContextProtocol
from ideasdk.utils import Utils

DEFAULT_ENFORCEMENT_INTERVAL = 10


class ThreadPoolMaxCapacity(Exception):
    pass


class WorkerThread:
    def __init__(
        self,
        context: SocaContextProtocol,
        thread_pool_name: str,
        worker_id: int,
        worker_queue: queue.Queue,
    ):
        self.context = context
        self._thread_pool_name = thread_pool_name
        self._worker_id = worker_id
        self._logger = context.logger(self.get_name())
        self._worker_queue = worker_queue
        self._exit = Event()
        self._idle = Event()
        self._thread = Thread(name=self.get_name(), target=self.loop)

    def get_name(self) -> str:
        return f'{self._thread_pool_name}-worker-{self._worker_id}'

    def is_active(self) -> bool:
        return not self._exit.is_set() and self._thread.is_alive()

    def is_idle(self) -> bool:
        return self._idle.is_set()

    def loop(self):
        while not self._exit.is_set():
            try:
                fn, args, kwargs = self._worker_queue.get(timeout=0.1)
                self._idle.clear()
                fn(*args, **kwargs)
            except queue.Empty:
                pass
            except Exception as e:
                self._logger.error(f'worker execution error: {e}')
            finally:
                self._idle.set()

    def start(self):
        self._logger.debug('Starting threads...')
        self._thread.start()

    def stop(self, wait=False):
        self._logger.debug('Stopping threads...')
        self._exit.set()
        if wait and self.is_active():
            self._thread.join()


class ThreadPoolExecutor:
    """
    Thread Pool Executor
    """

    def __init__(
        self,
        context: SocaContextProtocol,
        thread_pool_name: str,
        min_workers: int,
        max_workers: int,
        enforcement_interval: int = DEFAULT_ENFORCEMENT_INTERVAL,
        debug: bool = False,
    ):
        self._context = context
        self._logger = context.logger(thread_pool_name)
        self._thread_pool_name = thread_pool_name
        self._min_workers = min_workers
        self._max_workers = max_workers
        self._enforcement_interval = Utils.get_as_int(
            enforcement_interval, DEFAULT_ENFORCEMENT_INTERVAL
        )
        self._debug = debug

        self._worker_id_counter = FastWriteCounter(init=1)
        self._executor_thread = Thread(name=thread_pool_name, target=self._loop)
        self._all_workers: Set[WorkerThread] = set()
        self._active_workers: List[WorkerThread] = []
        self._exit = Event()
        self._worker_queue = queue.Queue(maxsize=max_workers)

    def _get_next_worker_id(self) -> int:
        worker_id = self._worker_id_counter.value
        self._worker_id_counter.increment()
        return worker_id

    def _cleanup_inactive_workers(self):
        if Utils.is_empty(self._all_workers):
            return

        workers_to_remove = []
        for worker in self._all_workers:
            if worker.is_active():
                continue
            workers_to_remove.append(worker)

        for worker in workers_to_remove:
            self._all_workers.remove(worker)

    def _loop(self):
        while not self._exit.is_set():
            try:
                self._cleanup_inactive_workers()
                self._adjust_worker_counts()
            except Exception as e:
                self._logger.exception(f'failed to adjust workers: {e}')
            finally:
                self._exit.wait(self._enforcement_interval)

    def get_worker_counts(
        self,
    ) -> Tuple[int, int, int, int, int, int, List[WorkerThread]]:
        idle_worker_count = 0
        idle_workers = []
        for worker in self._active_workers:
            if worker.is_idle():
                idle_worker_count += 1
                idle_workers.append(worker)
        required_worker_count = self._worker_queue.qsize()
        active_worker_count = len(self._all_workers)
        total_worker_count = len(self._active_workers)
        return (
            idle_worker_count,
            required_worker_count,
            active_worker_count,
            total_worker_count,
            self._min_workers,
            self._max_workers,
            idle_workers,
        )

    def _create_worker(self):
        worker = WorkerThread(
            context=self._context,
            thread_pool_name=self._thread_pool_name,
            worker_id=self._get_next_worker_id(),
            worker_queue=self._worker_queue,
        )
        if self._debug:
            self._logger.debug(f'starting worker: {worker.get_name()}')
        worker.start()
        self._all_workers.add(worker)
        self._active_workers.append(worker)

    def _adjust_worker_counts(self):
        (
            idle_worker_count,
            required_worker_count,
            active_worker_count,
            total_worker_count,
            min_worker_count,
            max_worker_count,
            idle_workers,
        ) = self.get_worker_counts()

        if self._debug:
            self._logger.debug(
                f'workers - '
                f'total: {total_worker_count}, '
                f'active: {active_worker_count}, '
                f'idle: {idle_worker_count}, '
                f'required: {required_worker_count}, '
                f'min: {min_worker_count}, '
                f'max: {max_worker_count}'
            )

        if (
            idle_worker_count > required_worker_count
            and idle_worker_count > min_worker_count
        ):
            # stop idle workers
            if self._debug:
                self._logger.debug(
                    f'active workers: {total_worker_count}, max: {max_worker_count}. terminating {(total_worker_count - required_worker_count)}'
                )

            for worker in idle_workers:
                if len(self._active_workers) <= min_worker_count:
                    break
                self._active_workers.remove(worker)
                if self._debug:
                    self._logger.debug(f'stopping worker: {worker.get_name()}')
                worker.stop(wait=False)

        elif idle_worker_count < required_worker_count:
            if active_worker_count >= max_worker_count:
                return

            # provision new workers.
            if self._debug:
                self._logger.debug(
                    f'active workers: {active_worker_count}, required: {required_worker_count} ...'
                )
            for _ in range(required_worker_count - idle_worker_count):
                self._create_worker()

    def submit(self, fn, /, *args, **kwargs):
        try:
            if (
                len(self._active_workers) + self._worker_queue.qsize()
                >= self._max_workers
            ):
                self._logger.debug(
                    ' Active+worker_queue.qsize() >= max_workers - raising ThreadPoolMaxCapacity'
                )
                raise ThreadPoolMaxCapacity

            self._worker_queue.put((fn, args, kwargs))
        except queue.Full:
            self._logger.debug('Queue full - raising ThreadPoolMaxCapacity')
            raise ThreadPoolMaxCapacity

    def start(self):
        self._logger.info(f'Starting Thread pool {self._thread_pool_name} ...')
        self._executor_thread.start()
        for _ in range(self._min_workers):
            self._create_worker()

    def stop(self, wait=True):
        self._logger.info(f'Stopping Thread pool {self._thread_pool_name} ...')
        self._exit.set()

        if wait:
            self._executor_thread.join()

        if Utils.is_not_empty(self._all_workers):
            for worker in self._all_workers:
                worker.stop(wait=wait)
