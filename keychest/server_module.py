#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Basic server module skeleton
"""

from past.builtins import cmp

from . import util
from .config import Config
from .redis_queue import RedisQueue
from . import redis_helper as rh
from .trace_logger import Tracelogger
from .errors import Error, InvalidHostname, ServerShuttingDown
from .server_jobs import JobTypes, BaseJob, PeriodicJob, PeriodicReconJob, PeriodicIpScanJob, ScanResults
from .consts import CertSigAlg, BlacklistRuleType, DbScanType, JobType, CrtshInputType, DbLastScanCacheType, IpType

import time
import json
import logging
import threading
import collections
from queue import Queue, Empty as QEmpty, Full as QFull, PriorityQueue


logger = logging.getLogger(__name__)


class ServerModule(object):
    """
    Server module
    """

    def __init__(self, *args, **kwargs):
        self.server = None
        self.db = None
        self.config = None
        self.trace_logger = Tracelogger(logger)

    def init(self, server):
        """
        Initializes module with the server
        :param server:
        :return:
        """
        self.server = server
        self.db = server.db
        self.config = server.config

    def shutdown(self):
        """
        Shutdown operation
        :return:
        """
        pass

    def is_running(self):
        """
        Returns true if server is still running
        :return:
        """
        return self.server.is_running()

    def run(self):
        """
        Kick off all running threads
        :return:
        """

    def periodic_feeder(self, s):
        """
        Server module can feed periodic tasks to the periodic worker.
        :param s: session
        :return:
        """

    def process_periodic_job(self, job):
        """
        Processes periodic job.
        When True is returned, job is consumed and should not be propagated further.

        :param job:
        :return:
        """
        # Return non-consumed (False) by default so the module doesn't eat jobs
        return False

    def periodic_job_update_last_scan(self, job):
        """
        Update last stan of the job
        :param job:
        :return: True if job was consumed
        """
        # Return non-consumed (False) by default so the module doesn't eat jobs
        return False

