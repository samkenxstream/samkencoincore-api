"""
Simple in-memory tracking of executed tasks.
"""

import datetime as dt
import logging
from enum import Enum
from multiprocessing import Process

import requests

from ingestion_server import slack
from ingestion_server.indexer import TableIndexer, elasticsearch_connect
from ingestion_server.ingest import reload_upstream


class TaskTypes(Enum):
    # Completely reindex all data for a given model.
    REINDEX = 0
    # Reindex updates to a model from the database since a certain date.
    UPDATE_INDEX = 1
    # Download the latest copy of the data from the upstream database, then
    # completely reindex the newly imported data.
    INGEST_UPSTREAM = 2
    # Create indices in Elasticsearch for QA tests.
    # This is not intended for production use, but can be safely executed in a
    # production environment without consequence.
    LOAD_TEST_DATA = 3


class TaskTracker:
    def __init__(self):
        self.id_task = {}
        self.id_action = {}
        self.id_progress = {}
        self.id_start_time = {}
        self.id_finish_time = {}
        self.id_active_workers = {}

    def add_task(self, task, task_id, action, progress, finish_time, active_workers):
        self._prune_old_tasks()
        self.id_task[task_id] = task
        self.id_action[task_id] = action
        self.id_progress[task_id] = progress
        self.id_start_time[task_id] = dt.datetime.utcnow().timestamp()
        self.id_finish_time[task_id] = finish_time
        self.id_active_workers[task_id] = active_workers
        return task_id

    def _prune_old_tasks(self):
        pass

    def list_task_statuses(self):
        self._prune_old_tasks()
        results = []
        for _id, task in self.id_task.items():
            percent_completed = self.id_progress[_id].value
            active = task.is_alive()
            start_time = self.id_start_time[_id]
            finish_time = self.id_finish_time[_id].value
            active_workers = self.id_active_workers[_id].value
            results.append(
                {
                    "task_id": _id,
                    "active": active,
                    "action": self.id_action[_id],
                    "progress": percent_completed,
                    "error": percent_completed < 100 and not active,
                    "start_time": start_time,
                    "finish_time": finish_time,
                    "active_workers": bool(active_workers),
                }
            )
        sorted_results = sorted(results, key=lambda x: x["finish_time"])

        to_utc = dt.datetime.utcfromtimestamp

        def render_date(x):
            return to_utc(x) if x != 0.0 else None

        # Convert date to a readable format
        for idx, task in enumerate(sorted_results):
            start_time = task["start_time"]
            finish_time = task["finish_time"]
            sorted_results[idx]["start_time"] = str(render_date(start_time))
            sorted_results[idx]["finish_time"] = str(render_date(finish_time))

        return sorted_results


class Task(Process):
    def __init__(
        self,
        model,
        task_type,
        since_date,
        progress,
        task_id,
        finish_time,
        active_workers,
        callback_url,
    ):
        Process.__init__(self)
        self.model = model
        self.task_type = task_type
        self.since_date = since_date
        self.progress = progress
        self.task_id = task_id
        self.finish_time = finish_time
        self.active_workers = active_workers
        self.callback_url = callback_url

    def run(self):
        try:
            # Map task types to actions.
            elasticsearch = elasticsearch_connect()
            indexer = TableIndexer(
                elasticsearch,
                self.model,
                self.task_id,
                self.progress,
                self.finish_time,
                self.active_workers,
            )
            if self.task_type == TaskTypes.REINDEX:
                slack.verbose(f"`{self.model}`: Beginning Elasticsearch reindex")
                indexer.reindex(self.model)
            elif self.task_type == TaskTypes.UPDATE_INDEX:
                indexer.update(self.model, self.since_date)
            elif self.task_type == TaskTypes.INGEST_UPSTREAM:
                reload_upstream(self.model)
                if self.model == "audio":
                    reload_upstream("audioset", approach="basic")
                indexer.reindex(self.model)
            elif self.task_type == TaskTypes.LOAD_TEST_DATA:
                indexer.load_test_data(self.model)
            logging.info(f"Task {self.task_id} exited.")
            if self.callback_url:
                try:
                    logging.info("Sending callback request")
                    res = requests.post(self.callback_url)
                    logging.info(f"Response: {res.text}")
                except requests.exceptions.RequestException as e:
                    logging.error("Failed to send callback!")
                    logging.error(e)
        except Exception as err:
            exception_type = f"{err.__class__.__module__}.{err.__class__.__name__}"
            slack.error(
                f":x_red: Error processing task `{self.task_type}` for `{self.model}` "
                f"(`{exception_type}`): \n"
                f"```\n{err}\n```"
            )
            raise
