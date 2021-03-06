#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Server part of the script
NOTE: this file will be refactored soon, pls any edits consult with maintainer
"""

import argparse
import base64
import collections
import json
import logging
import math
import os
import random
import resource
import signal
import socket
import sys
import threading
import time
from datetime import datetime, timedelta
from queue import Queue, Empty as QEmpty, Full as QFull, PriorityQueue
from threading import RLock as RLock

import coloredlogs
import dns.resolver
import ph4whois
import pid
import sqlalchemy as salch
from events import Events
from sqlalchemy import case, literal_column
from sqlalchemy.orm.query import Query as SaQuery

from . import dbutil
from . import redis_helper as rh
from . import util
from . import util_cert
from .cert_path_validator import PathValidator, ValidationOsslException, ValidationResult
from .config import Config
from .consts import CertSigAlg, BlacklistRuleType, DbScanType, JobType, CrtshInputType, DbLastScanCacheType, IpType
from .core import Core
from .crt_sh_processor import CrtProcessor, CrtShException, CrtShTimeoutException
from .daemon import Daemon
from .db_migrations import DbMigrationManager
from .dbutil import MySQL, ScanJob, Certificate, CertificateAltName, DbCrtShQuery, DbCrtShQueryResult, \
    DbHandshakeScanJob, DbHandshakeScanJobResult, DbWatchTarget, DbWatchAssoc, DbBaseDomain, DbWhoisCheck, \
    DbScanHistory, DbHelper, ColTransformWrapper, \
    DbDnsResolve, DbCrtShQueryInput, \
    DbSubdomainResultCache, DbSubdomainScanBlacklist, DbSubdomainWatchAssoc, DbSubdomainWatchTarget, \
    DbSubdomainWatchResultEntry, DbDnsEntry, DbLastScanCache, DbWatchService, \
    DbDomainName, DbIpAddress, DbTlsScanDesc, DbTlsScanParams, DbTlsScanDescExt, \
    DbIpScanRecord, DbIpScanRecordUser, DbIpScanResult, \
    ResultModelUpdater, ModelUpdater
from .errors import Error, InvalidHostname, ServerShuttingDown
from .redis_client import RedisClient
from .redis_queue import RedisQueue
from .server_agent import ServerAgent
from .server_api import RestAPI
from .server_api_proc import ServerApiProc
from .server_jobs import JobTypes, BaseJob, PeriodicJob, PeriodicReconJob, PeriodicIpScanJob, ScanResults
from .server_key_tester import KeyTester
from .server_management import ManagementModule
from .stat_sem import StatSemaphore
from .tls_domain_tools import TlsDomainTools, TargetUrl, CnameCDNClassifier
from .tls_handshake import TlsHandshaker, TlsHandshakeResult, TlsTimeout, TlsResolutionError, TlsException, \
    TlsHandshakeErrors
from .tls_scanner import TlsScanner, RequestErrorCode
from .trace_logger import Tracelogger
from .pki_manager import PkiManager
from .pki_manager_le import PkiLeManager
from .certificate_manager import CertificateManager
from .database_manager import DatabaseManager

__author__ = 'dusanklinec'
logger = logging.getLogger(__name__)
coloredlogs.install(level=logging.INFO)


class AppDeamon(Daemon):
    """
    Daemon wrapper
    """
    def __init__(self, *args, **kwargs):
        Daemon.__init__(self, *args, **kwargs)
        self.app = kwargs.get('app')

    def run(self, *args, **kwargs):
        self.app.work()


#
# Servers
#


class Server(object):
    """
    Main server object
    """

    def __init__(self, *args, **kwargs):
        self.core = Core()
        self.args = None
        self.config = None

        self.logdir = '/var/log/enigma-keychest'
        self.piddir = '/var/run'

        self.daemon = None
        self.running = True
        self.run_thread = None
        self.stop_event = threading.Event()
        self.terminate = False
        self.agent_mode = False
        self.last_result = None

        self.db = None
        self.redis = None
        self.redis_queue = None

        self.job_queue = Queue(300)
        self.local_data = threading.local()
        self.workers = []

        self.watch_last_db_scan = 0
        self.watch_db_scan_period = 5
        self.watcher_job_queue_size = 512
        self.watcher_job_queue = PriorityQueue()
        self.watcher_db_cur_jobs = {}  # watchid -> job either in queue or processing
        self.watcher_db_processing = {}  # watchid -> time scan started, protected by lock
        self.watcher_db_lock = RLock()
        self.watcher_workers = []
        self.watcher_thread = None
        self.watcher_job_semaphores = {}  # semaphores for particular tasks

        self.sub_blacklist = {}
        self.sub_blacklist_lock = RLock()

        self.trace_logger = Tracelogger(logger)
        self.crt_sh_proc = CrtProcessor(timeout=8, attempts=2)
        self.tls_handshaker = TlsHandshaker(timeout=5, tls_version='TLS_1_2', attempts=3)
        self.crt_validator = PathValidator()
        self.domain_tools = TlsDomainTools()
        self.cname_cdn_classif = CnameCDNClassifier()
        self.tls_scanner = TlsScanner()
        self.pki_manager = PkiManager()
        self.cert_manager = CertificateManager()
        self.db_manager = DatabaseManager()
        self.test_timeout = 5
        self.api = None
        self.events = Events()

        self.modules = []
        self.mod_api_proc = None  # type: ServerApiProc
        self.mod_key_tester = None  # type: KeyTester
        self.mod_agent = None  # type: ServerAgent
        self.mod_mgmt = None  # type: ManagementModule

        self.cleanup_last_check = 0
        self.cleanup_check_time = 60
        self.cleanup_thread = None
        self.cleanup_thread_lock = RLock()

        self.state_thread = None
        self.state_last_check = 0

        self.randomize_diff_time_fact = 0.15
        self.randomize_feeder_fact = 0.25
        self.delta_dns = timedelta(hours=2)
        self.delta_tls = timedelta(hours=8)
        self.delta_crtsh = timedelta(hours=12)
        self.delta_whois = timedelta(hours=48)
        self.delta_wildcard = timedelta(days=2)
        self.delta_ip_scan = timedelta(days=2)

    def check_pid(self, retry=True):
        """
        Check the PID lock ownership
        :param retry:
        :return:
        """
        first_retry = True
        attempt_ctr = 0
        while first_retry or retry:
            try:
                first_retry = False
                attempt_ctr += 1

                self.core.pidlock_create()
                if attempt_ctr > 1:
                    print('\nPID lock acquired')
                return True

            except pid.PidFileAlreadyRunningError as e:
                return True

            except pid.PidFileError as e:
                pidnum = self.core.pidlock_get_pid()
                print('\nError: CLI already running in exclusive mode by PID: %d' % pidnum)

                if self.args.pidlock >= 0 and attempt_ctr > self.args.pidlock:
                    return False

                print('Next check will be performed in few seconds. Waiting...')
                time.sleep(3)
        pass

    def return_code(self, code=0):
        self.last_result = code
        return code

    def init_config(self):
        """
        Initializes configuration
        :return:
        """
        if self.args.ebstall:
            self.config = Config.from_file('/etc/enigma/config.json')
            self.config.mysql_user = 'keychest'
            return

        self.config = Core.read_configuration()
        if self.config is None or not self.config.has_nonempty_config():
            sys.stderr.write('Configuration is empty: %s\nCreating default one... (fill in access credentials)\n'
                             % Core.get_config_file_path())

            Core.write_configuration(Config.default_config())
            return self.return_code(1)

        if self.args.server_debug and self.args.daemon:
            # Server debug causes flask to restart the whole daemon (due to server reloading on code change)
            logger.error('Server debug and daemon are mutually exclusive')
            raise ValueError('Invalid start arguments')

        self.agent_mode = self.config.agent_mode
        if self.agent_mode and util.is_empty(self.config.master_endpoint):
            raise ValueError('Master endpoint is required in agent mode')

    def init_log(self):
        """
        Initializes logging
        :return:
        """
        util.make_or_verify_dir(self.logdir)

    def init_db(self):
        """
        Initializes the database
        :return:
        """
        self.db = MySQL(config=self.config)
        self.db.init_db()

        # redis init
        self.redis = RedisClient()
        self.redis.init(self.config)
        self.redis_queue = RedisQueue(redis_client=self.redis)

    def init_misc(self):
        """
        Misc components init
        :return: 
        """
        self.crt_validator.init()
        self.cname_cdn_classif.init()
        self.db_manager.init(db=self.db, config=self.config)
        self.cert_manager.init(db=self.db, config=self.config, db_manager=self.db_manager)
        self.pki_manager.init(db=self.db, config=self.config)

        le_pki_manager = PkiLeManager(self.pki_manager)
        le_pki_manager.register()

        signal.signal(signal.SIGINT, self.signal_handler)

    def init_modules(self):
        """
        Initializes modules for the server
        :return:
        """
        self.mod_api_proc = ServerApiProc()
        self.mod_api_proc.init(self)
        self.modules.append(self.mod_api_proc)

        self.mod_key_tester = KeyTester()
        self.mod_key_tester.init(self)
        self.modules.append(self.mod_key_tester)

        self.mod_agent = ServerAgent()
        self.mod_agent.init(self)
        self.modules.append(self.mod_agent)

        self.mod_mgmt = ManagementModule()
        self.mod_mgmt.init(self)
        self.modules.append(self.mod_mgmt)

    def signal_handler(self, signal, frame):
        """
        Signal handler - terminate gracefully
        :param signal:
        :param frame:
        :return:
        """
        logger.info('CTRL+C pressed')
        self.trigger_stop()

    def trigger_stop(self):
        """
        Sets terminal conditions to true
        :return:
        """
        self.terminate = True
        self.stop_event.set()
        if self.api:
            self.api.shutdown_server()

    def is_running(self):
        """
        Returns true if termination was not triggered
        :return: 
        """
        return not self.terminate and not self.stop_event.isSet()

    #
    # Interface - Redis interactive jobs
    #

    def process_redis_job(self, job):
        """
        Main redis job processor
        Handles job logic as implemented in Laravel.
        e.g., removes jobs from delay/reserved queues when finished.
        :param job: 
        :return: 
        """
        try:
            # Process job in try-catch so it does not break worker
            logger.info('New job: %s' % json.dumps(job.decoded, indent=4))
            rh.mark_failed_if_exceeds(job)

            # Here we will fire off the job and let it process. We will catch any exceptions so
            # they can be reported to the developers logs, etc. Once the job is finished the
            # proper events will be fired to let any listeners know this job has finished.
            self.on_redis_job(job)

            # Once done, delete job from the queue
            if not job.is_deleted_or_released():
                job.delete()

        except Exception as e:
            logger.error('Exception in processing job %s' % (e,))
            self.trace_logger.log(e)

            rh.mark_failed_exceeds_attempts(job, 5, e)
            if not job.is_deleted_or_released() and not job.failed:
                job.release()

    def on_redis_job(self, job):
        """
        Main redis job router. Determines which command should be executed.
        Run by the worker.
        :param job: 
        :return: 
        """
        payload = job.decoded
        if payload is None or 'id' not in payload or 'data' not in payload:
            logger.warning('Invalid job detected: %s' % json.dumps(payload))
            job.delete()
            return

        data = payload['data']
        cmd = data['commandName']
        if cmd == 'App\\Jobs\\ScanHostJob':
            self.on_redis_scan_job(job)
        elif cmd == 'App\\Jobs\\AutoAddSubsJob':
            self.on_redis_auto_sub_job(job)
        else:
            logger.warning('Unknown job: %s' % cmd)
            job.delete()
            return

    def on_redis_scan_job(self, job):
        """
        Redis spot check (scan) job
        Run by the worker.
        :param job: 
        :return: 
        """
        self.augment_redis_scan_job(job)

        job_data = job.decoded['data']['json']
        domain = job_data['scan_host']
        logger.debug(job_data)

        s = None
        self.update_scan_job_state(job_data, 'started')
        try:
            s = self.db.get_session()

            # load job object
            job_db = s.query(ScanJob).filter(ScanJob.uuid == job_data['uuid']).first()

            #
            # DNS scan
            db_dns, dns_entries = self.scan_dns(s, job_data, domain, job_db)
            s.commit()

            self.update_scan_job_state(job_db, 'dns-done', s)

            # pick default IP address for TLS scan
            if db_dns and db_dns.status == 1 and len(dns_entries) > 0:
                scan_ip = util.defvalkey(job_data, 'scan_ip', default=None, take_none=False)
                if util.is_empty(scan_ip):
                    job_data['scan_ip'] = dns_entries[0].ip

            #
            # crt.sh scan - only if DNS is correct
            if db_dns and db_dns.status == 1:
                try:
                    self.scan_crt_sh(s, job_data, domain, job_db)
                    s.commit()

                except Exception as e:
                    logger.debug('Exception in UI crtsh scan: %s - %s' % (domain, e))
                    self.trace_logger.log(e)

            self.update_scan_job_state(job_db, 'crtsh-done', s)

            #
            # TLS direct host scan
            if db_dns and db_dns.status == 1:
                self.scan_handshake(s, job_data, domain, job_db)
                s.commit()

            self.update_scan_job_state(job_db, 'tls-done', s)

            # Whois scan - only if DNS was done correctly
            if db_dns and db_dns.status == 1:
                self.scan_whois(s, job_data, domain, job_db)

            # final commit
            s.commit()

        except Exception as e:
            logger.warning('Scanning job exception: %s' % e)
            self.trace_logger.log(e)

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

        self.update_scan_job_state(job_data, 'finished')
        pass

    def on_redis_auto_sub_job(self, job):
        """
        Redis job for auto-add sub domains.
        Run by the worker.
        :param job:
        :return:
        """
        self.augment_redis_scan_job(job)

        job_data = job.decoded['data']['json']
        assoc_id = job_data['id']

        s = None
        try:
            s = self.db.get_session()

            assoc = s.query(DbSubdomainWatchAssoc).filter(DbSubdomainWatchAssoc.id == assoc_id).first()
            if assoc is None:
                return

            self.auto_fill_assoc(s, assoc)
            s.commit()

        except Exception as e:
            logger.warning('Auto add sub job exception: %s' % e)
            self.trace_logger.log(e)

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def augment_redis_scan_job(self, job=None, data=None):
        """
        Augments job with retry counts, timeouts and so on.
        :param RedisJob job:
        :param data:
        :return:
        """
        if job is not None:
            data = job.decoded['data']['json']

        scan_type = None
        if 'scan_type' in data:
            scan_type = data['scan_type']

        sys_params = collections.OrderedDict()
        sys_params['retry'] = 1
        sys_params['timeout'] = 4
        sys_params['mode'] = JobType.UI

        if scan_type == 'planner':
            sys_params['retry'] = 2
            sys_params['timeout'] = 15  # tls & connect scan
            sys_params['mode'] = JobType.BACKGROUND

        data['sysparams'] = sys_params
        return data

    #
    # Scans
    #

    def scan_handshake(self, s, job_data, query, job_db, store_job=True, **kwargs):
        """
        Performs direct handshake if applicable
        :param s: 
        :param job_data: 
        :param query: 
        :param job_db:
        :type job_db ScanJob
        :param store_job: stores job to the database in the scanning process.
                          Not storing the job immediately has meaning for diff scanning (watcher).
                          Gathered certificates are stored always.
        :param kwargs
        :return:
        :rtype (TlsHandshakeResult, DbHandshakeScanJob)
        """
        domain = job_data['scan_host']
        domain_sni = util.defvalkey(job_data, 'scan_sni', domain, take_none=False)
        scan_ip = util.defvalkey(job_data, 'scan_ip', None)

        if scan_ip is not None and not TlsDomainTools.is_ip(scan_ip):
            logger.debug('Invalid IP %s' % scan_ip)
            return
        elif scan_ip is not None:
            domain = scan_ip

        sys_params = job_data['sysparams']
        if not TlsDomainTools.can_connect(domain):
            logger.debug('Domain %s not elligible to handshake' % domain)
            return

        port = int(util.defvalkey(job_data, 'scan_port', 443, take_none=False))
        scheme = util.defvalkey(job_data, 'scan_scheme', None, take_none=False)
        do_connect_analysis = util.defvalkey(job_data, 'dns_ok', True, take_none=False)

        if 'do_connect_analysis' in kwargs:  # can only disable, if DNS failed, cannot perform
            do_connect_analysis &= kwargs.get('do_connect_analysis')

        do_process_certificates = kwargs.get('do_process_certificates', True)

        # Simple TLS handshake to the given host.
        # Analyze results, store scan record.
        try:
            resp = None  # type: TlsHandshakeResult
            try:
                resp = self.tls_handshaker.try_handshake(domain, port, scheme=scheme,
                                                         attempts=sys_params['retry'],
                                                         timeout=sys_params['timeout'],
                                                         domain=domain_sni)

            except TlsTimeout as te:
                logger.debug('Scan timeout: %s' % te)
                resp = te.scan_result
            except TlsResolutionError as te:
                logger.debug('Scan resolution errors: %s' % te)
                resp = te.scan_result
            except TlsException as te:
                logger.debug('Scan fail: %s' % te)
                resp = te.scan_result

            logger.debug(resp)
            time_elapsed = None
            if resp.time_start is not None and resp.time_finished is not None:
                time_elapsed = (resp.time_finished - resp.time_start)*1000
            if time_elapsed is None and resp.time_start is not None and resp.time_failed is not None:
                time_elapsed = (resp.time_failed - resp.time_start)*1000

            # scan record
            scan_db = DbHandshakeScanJob()
            scan_db.created_at = salch.func.now()
            scan_db.job_id = job_db.id if job_db is not None else None
            scan_db.ip_scanned = resp.ip if resp.ip is not None else '-'  # placeholder IP, group by fix
            scan_db.is_ipv6 = TlsDomainTools.is_valid_ipv6_address(scan_db.ip_scanned)
            scan_db.tls_ver = resp.tls_version
            scan_db.status = len(resp.certificates) > 0
            scan_db.err_code = resp.handshake_failure
            scan_db.tls_alert_code = resp.alert.desc if resp.alert else None
            scan_db.time_elapsed = time_elapsed
            scan_db.results = len(resp.certificates)
            scan_db.new_results = 0
            if store_job:
                s.add(scan_db)
                s.flush()

            # Certificates processing + cert path validation
            if do_process_certificates:
                self.process_handshake_certs(s, resp, scan_db, do_job_subres=store_job)

            # Cert validity
            self.tls_cert_validity_test(resp=resp, scan_db=scan_db)
            if store_job:
                s.flush()

            # Reverse IP lookup
            if scan_db.ip_scanned is not None and scan_db.ip_scanned != '-':
                self.reverse_ip_analysis(s, sys_params, resp, scan_db, domain_sni, job_data=job_data)

            # Try direct connect with requests, follow urls
            if do_connect_analysis:
                self.connect_analysis(s, sys_params, resp, scan_db, domain_sni, port, scheme, job_data=job_data)
            else:
                logger.debug('Connect analysis skipped for %s' % domain_sni)

            return resp, scan_db

        except Exception as e:
            logger.debug('Exception when scanning: %s' % e)
            self.trace_logger.log(e)
        return None, None

    def scan_crt_sh(self, s, job_data, query, job_db, store_to_db=True):
        """
        Performs one simple CRT SH scan with the given query
        stores the results.
        
        :param s: 
        :param job_data: 
        :param query:
        :param job_db:
        :type job_db ScanJob
        :param store_to_db if true results are stored to the database, otherwise just returned.
               Gathered certificates are stored always.

        :return:
        :rtype Tuple[DbCrtShQuery, List[DbCrtShQueryResult]]
        """
        crt_sh = None
        raw_query = self.get_crtsh_text_query(query)
        query_type = self.get_crtsh_query_type(query)
        job_type = self.get_job_type(job_data)

        # wildcard background scan - higher timeout
        scan_kwargs = {}
        cert_load_count = 500  # 500 certificates in one batch
        if job_type == JobType.BACKGROUND:
            if query_type == CrtshInputType.LIKE_WILDCARD:
                scan_kwargs['timeout'] = 20
            if query_type == CrtshInputType.EXACT:
                scan_kwargs['timeout'] = 10
        elif job_type == JobType.UI:
            cert_load_count = 30

        try:
            crt_sh = self.crt_sh_proc.query(raw_query, **scan_kwargs)

        except CrtShTimeoutException as tex:
            logger.warning('CRTSH timeout for: %s' % raw_query)
            raise

        if crt_sh is None:
            raise CrtShException('CRTSH returned empty result for %s' % raw_query)

        # existing certificates - have pem
        all_crt_ids = set([int(x.id) for x in crt_sh.results if x is not None and x.id is not None])
        existing_ids = self.cert_manager.cert_load_existing(s, list(all_crt_ids))  # type: dict[int -> int]  # crtsh id -> db id
        existing_ids_set = set(existing_ids.keys())
        new_ids = all_crt_ids - existing_ids_set

        # certificate ids (database IDs)
        certs_ids = list(existing_ids.values())

        # scan record
        crtsh_query_db = DbCrtShQuery()
        crtsh_query_db.created_at = salch.func.now()
        crtsh_query_db.job_id = job_db.id if job_db is not None else None
        crtsh_query_db.status = crt_sh.success
        crtsh_query_db.results = len(all_crt_ids)
        crtsh_query_db.new_results = len(new_ids)

        # input
        db_input, inp_is_new = self.db_manager.get_crtsh_input(s, query)
        if db_input is not None:
            crtsh_query_db.input_id = db_input.id

        if store_to_db:
            s.add(crtsh_query_db)
            s.flush()
            if job_db is not None:
                job_db.crtsh_check_id = crtsh_query_db.id
                job_db = s.merge(job_db)

        # existing records
        sub_res_list = []
        for crt_sh_id in existing_ids:
            crtsh_res_db = DbCrtShQueryResult()
            crtsh_res_db.query_id = crtsh_query_db.id
            crtsh_res_db.job_id = crtsh_query_db.job_id
            crtsh_res_db.crt_id = existing_ids[crt_sh_id]
            crtsh_res_db.crt_sh_id = crt_sh_id
            crtsh_res_db.was_new = 0
            sub_res_list.append(crtsh_res_db)
            if store_to_db:
                s.add(crtsh_res_db)

        # load pem for new certificates
        for new_crt_id in sorted(list(new_ids), reverse=True)[:cert_load_count]:
            db_cert, subres = \
                self.fetch_new_certs(s, job_data, new_crt_id,
                                     [x for x in crt_sh.results if int(x.id) == new_crt_id][0],
                                     crtsh_query_db, store_res=store_to_db)
            if db_cert is not None:
                certs_ids.append(db_cert.id)
            if subres is not None:
                sub_res_list.append(subres)

        for cert in crt_sh.results:
            self.analyze_cert(s, job_data, cert)

        crtsh_query_db.certs_ids = json.dumps(sorted(certs_ids))
        crtsh_query_db.certs_sh_ids = json.dumps(sorted(all_crt_ids))
        crtsh_query_db.newest_cert_id = max(certs_ids) if not util.is_empty(certs_ids) else None
        crtsh_query_db.newest_cert_sh_id = max(all_crt_ids) if not util.is_empty(certs_ids) else None
        return crtsh_query_db, sub_res_list

    def scan_whois(self, s, job_data, query, job_db, store_to_db=True):
        """
        Performs whois scan if applicable
        :param s:
        :param job_data:
        :param query:
        :param job_db:
        :type job_db ScanJob
        :param store_to_db: stores job to the database in the scanning process.
                          Not storing the job immediately has meaning for diff scanning (watcher).
        :return:
        :rtype DbWhoisCheck
        """
        domain = job_data['scan_host']
        sys_params = job_data['sysparams']
        if not TlsDomainTools.can_whois(domain):
            logger.debug('Domain %s not elligible to whois scan' % domain)
            return

        try:
            top_domain = TlsDomainTools.get_top_domain(domain)
            top_domain_db, domain_new = self.db_manager.load_top_domain(s, top_domain=top_domain)

            last_scan = self.load_last_whois_scan(s, top_domain_db) if not domain_new else None
            if last_scan is not None \
                    and last_scan.last_scan_at \
                    and last_scan.last_scan_at > self.diff_time(self.delta_whois, rnd=True):
                if job_db is not None:
                    job_db.whois_check_id = last_scan.id
                    job_db = s.merge(job_db)
                return last_scan

            scan_db = DbWhoisCheck()
            scan_db.domain = top_domain_db
            scan_db.last_scan_at = datetime.now()
            scan_db.created_at = salch.func.now()
            scan_db.updated_at = salch.func.now()
            resp = None
            try:
                resp = self.try_whois(top_domain, attempts=sys_params['retry'])
                if resp is None:  # not found
                    scan_db.status = 2
                else:
                    scan_db.registrant_cc = util.utf8ize(util.first(resp.country))
                    scan_db.registrar = util.utf8ize(util.first(resp.registrar))
                    scan_db.expires_at = util.first(resp.expiration_date)
                    scan_db.registered_at = util.first(resp.creation_date)
                    scan_db.rec_updated_at = util.first(resp.updated_date)
                    scan_db.dnssec = not util.is_empty(resp.dnssec) and resp.dnssec != 'unsigned'
                    scan_db.dns = json.dumps(util.lower(util.strip(
                        sorted(util.try_list(resp.name_servers)))))
                    scan_db.emails = json.dumps(util.lower(util.strip(
                        sorted(util.try_list(resp.emails)))))
                    scan_db.status = 1

            except ph4whois.parser.PywhoisSlowDownError as se:
                scan_db.status = 3
                logger.debug('Whois scan fail - slow down: %s' % se)
                self.trace_logger.log(se, custom_msg='Whois exception')

            except Exception as e:
                scan_db.status = 0
                logger.debug('Whois scan fail: %s' % e)
                self.trace_logger.log(e, custom_msg='Whois exception')

            if store_to_db and scan_db.status != 3:
                s.add(scan_db)
                s.flush()
                if job_db is not None:
                    job_db.whois_check_id = scan_db.id
                    job_db = s.merge(job_db)

            return scan_db

        except Exception as e:
            logger.debug('Exception in whois scan: %s' % e)
            self.trace_logger.log(e)

    def scan_dns(self, s, job_data, query, job_db, store_to_db=True):
        """
        Performs DNS scan
        :param s:
        :param job_data:
        :param query:
        :param job_db:
        :type job_db Optional[ScanJob]
        :param store_to_db: stores job to the database in the scanning process.
                          Not storing the job immediately has meaning for diff scanning (watcher).
        :return:
        :rtype DbDnsResolve
        """
        domain = job_data['scan_host']
        watch_id = util.defvalkey(job_data, 'watch_id')
        is_ip = TlsDomainTools.get_ip_type(domain)

        if is_ip == IpType.NOT_IP and not TlsDomainTools.can_whois(domain):
            logger.debug('Domain %s not elligible to DNS scan' % domain)
            return

        scan_db = DbDnsResolve()
        scan_db.watch_id = watch_id
        scan_db.job_id = job_db.id if job_db is not None else None
        scan_db.last_scan_at = datetime.now()
        scan_db.created_at = salch.func.now()
        scan_db.updated_at = salch.func.now()

        try:
            if is_ip != IpType.NOT_IP:
                # Synthetic DNS resolution - unification mechanism for IP based watches
                # (same fetch queries for all targets)
                res = [(TlsDomainTools.get_ip_family(type_idx=is_ip), domain)]
                scan_db.is_synthetic = True
                
            else:
                results = socket.getaddrinfo(domain, 443,
                                             0,
                                             socket.SOCK_STREAM,
                                             socket.IPPROTO_TCP)

                res = []
                for cur in results:
                    res.append((cur[0], cur[4][0]))

            scan_db.dns_res = res
            scan_db.dns_status = 1
            scan_db.status = 1
            scan_db.dns = json.dumps(res)
            scan_db.num_res = len(scan_db.dns_res)
            scan_db.num_ipv4 = len([x for x in scan_db.dns_res if x[0] == 2])
            scan_db.num_ipv6 = len([x for x in scan_db.dns_res if x[0] == 10])

        except socket.gaierror as gai:
            logger.debug('GAI error: %s: %s' % (domain, gai))
            scan_db.status = 2
            scan_db.dns_status = 2

        except Exception as e:
            logger.debug('Exception in DNS scan: %s : %s' % (domain, e))
            scan_db.status = 3
            scan_db.dns_status = 3
            self.trace_logger.log(e)

        # CNAME resolution
        try:
            if is_ip == IpType.NOT_IP:
                my_resolver = dns.resolver.Resolver()
                my_resolver.timeout = 4
                cnames = list(my_resolver.query(domain, 'CNAME'))
                if len(cnames) > 0:
                    scan_db.cname = util.remove_trailing_char(cnames[0].to_text(), '.')
                    job_data['cname'] = scan_db.cname

        except dns.resolver.NoAnswer:
            pass  # no cname

        except dns.resolver.NXDOMAIN:
            pass  # no CNAME

        except dns.resolver.Timeout:
            pass  # resolver timeout

        except Exception as e:
            logger.debug('Exception in DNS scan: %s : %s' % (domain, e))
            self.trace_logger.log(e)

        if store_to_db:
            s.add(scan_db)
            s.flush()
            if job_db is not None:
                job_db.dns_check_id = scan_db.id
                job_db = s.merge(job_db)

        # DNS sub entries
        dns_entries = []
        for idx, tup in enumerate(scan_db.dns_res):
            family, addr = tup
            entry = DbDnsEntry()
            entry.is_ipv6 = family == 10
            entry.is_internal = TlsDomainTools.is_ip_private(addr)
            entry.ip = addr
            entry.res_order = idx
            entry.scan_id = scan_db.id

            dns_entries.append(entry)
            if store_to_db:
                s.add(entry)
        s.flush()

        return scan_db, dns_entries

    #
    # Periodic scanner
    #

    def load_active_watch_targets(self, s, last_scan_margin=300, randomize=True):
        """
        Loads active jobs to scan, from the oldest.
        After loading the result is a tuple (DbWatchTarget, min_periodicity).

        select wt.*, min(uw.scan_periodicity) from user_watch_target uw
            inner join watch_target wt on wt.id = uw.watch_id
            where uw.deleted_at is null
            group by wt.id, uw.scan_type
            order by last_scan_state desc;
        :param s : SaQuery query
        :type s: SaQuery
        :param last_scan_margin: margin for filtering out records that were recently processed.
        :param randomize: randomizes margin +- 25%
        :return:
        """
        q = s.query(
                    DbWatchTarget,
                    salch.func.min(DbWatchAssoc.scan_periodicity).label('min_periodicity'),
                    DbWatchService
        )\
            .select_from(DbWatchAssoc)\
            .join(DbWatchTarget, DbWatchAssoc.watch_id == DbWatchTarget.id)\
            .outerjoin(DbWatchService, DbWatchService.id == DbWatchTarget.service_id)\
            .filter(DbWatchAssoc.deleted_at == None)\
            .filter(DbWatchAssoc.disabled_at == None)

        if last_scan_margin:
            if randomize:
                fact = randomize if isinstance(randomize, float) else self.randomize_feeder_fact
                last_scan_margin += math.ceil(last_scan_margin * random.uniform(-1*fact, fact))
            cur_margin = datetime.now() - timedelta(seconds=last_scan_margin)
            q = q.filter(salch.or_(
                DbWatchTarget.last_scan_at < cur_margin,
                DbWatchTarget.last_scan_at == None
            ))

        return q.group_by(DbWatchTarget.id, DbWatchAssoc.scan_type)\
                .order_by(DbWatchTarget.last_scan_at)  # select the oldest scanned first

    def load_active_recon_targets(self, s, last_scan_margin=300, randomize=True):
        """
        Loads active jobs to scan, from the oldest.
        After loading the result is a tuple (DbSubdomainWatchTarget, min_periodicity).

        :param s : SaQuery query
        :type s: SaQuery
        :param last_scan_margin: margin for filtering out records that were recently processed.
        :param randomize:
        :return:
        """
        q = s.query(
            DbSubdomainWatchTarget,
                    salch.func.min(DbSubdomainWatchAssoc.scan_periodicity).label('min_periodicity')
        )\
            .select_from(DbSubdomainWatchAssoc)\
            .join(DbSubdomainWatchTarget, DbSubdomainWatchAssoc.watch_id == DbSubdomainWatchTarget.id)\
            .filter(DbSubdomainWatchAssoc.deleted_at == None)

        if last_scan_margin:
            if randomize:
                fact = randomize if isinstance(randomize, float) else self.randomize_feeder_fact
                last_scan_margin += math.ceil(last_scan_margin * random.uniform(-1*fact, fact))
            cur_margin = datetime.now() - timedelta(seconds=last_scan_margin)
            q = q.filter(salch.or_(
                DbSubdomainWatchTarget.last_scan_at < cur_margin,
                DbSubdomainWatchTarget.last_scan_at == None
            ))

        return q.group_by(DbSubdomainWatchTarget.id, DbSubdomainWatchAssoc.scan_type)\
                .order_by(DbSubdomainWatchTarget.last_scan_at)  # select the oldest scanned first

    def load_active_ip_scan_targets(self, s, last_scan_margin=300, randomize=True):
        """
        Loads active IP scan jobs to scan, from the oldest.
        After loading the result is a tuple (DbIpScanRecordUser, min_periodicity).

        :param s : SaQuery query
        :type s: SaQuery
        :param last_scan_margin: margin for filtering out records that were recently processed.
        :param randomize:
        :return:
        """
        q = s.query(
            DbIpScanRecord,
            salch.func.min(DbIpScanRecordUser.scan_periodicity).label('min_periodicity')
        ) \
            .select_from(DbIpScanRecordUser) \
            .join(DbIpScanRecord, DbIpScanRecordUser.ip_scan_record_id == DbIpScanRecord.id) \
            .filter(DbIpScanRecordUser.deleted_at == None)

        if last_scan_margin:
            if randomize:
                fact = randomize if isinstance(randomize, float) else self.randomize_feeder_fact
                last_scan_margin += math.ceil(last_scan_margin * random.uniform(-1 * fact, fact))
            cur_margin = datetime.now() - timedelta(seconds=last_scan_margin)
            q = q.filter(salch.or_(
                DbIpScanRecord.last_scan_at < cur_margin,
                DbIpScanRecord.last_scan_at == None
            ))

        return q.group_by(DbIpScanRecord.id) \
            .order_by(DbIpScanRecord.last_scan_at)  # select the oldest scanned first

    def min_scan_margin(self):
        """
        Computes minimal scan margin from the scan timeouts
        :return:
        """
        return min(self.delta_dns, self.delta_tls, self.delta_crtsh, self.delta_whois).total_seconds()

    def periodic_queue_is_full(self, for_who=None):
        """
        Returns true if the main watcher queue is full.
        The queue is not strictly bounded to avoid exceptions on re-adding failed
        job to the queue for retry. Method though returns inaccurate results. Used for
        job management.
        :param: for_who:
        :return:
        """
        size = self.watcher_job_queue.qsize()
        limit = self.watcher_job_queue_size

        # make some space for next feeders so it does not starve
        if for_who is not None and for_who == JobTypes.SUB:
            limit += math.ceil(0.1 * limit)

        return size >= limit

    def periodic_queue_should_add_new(self):
        """
        Returns true if new jobs can be added to the queue.
        :return:
        """
        size = self.watcher_job_queue.qsize()
        return size <= self.watcher_job_queue_size * 0.85

    def periodic_feeder_init(self):
        """
        Initializes data structures required for data processing
        :return:
        """
        num_max_recon = max(1, min(self.config.periodic_workers, int(self.config.periodic_workers * 0.15 + 1)))  # 15 %
        num_max_ips = max(1, min(self.config.periodic_workers, int(self.config.periodic_workers * 0.10 + 1)))  # 10 %
        num_max_api = max(1, min(self.config.periodic_workers, int(self.config.periodic_workers * 0.10 + 1)))  # 10 %
        num_max_others = max(1, min(self.config.periodic_workers, int(self.config.periodic_workers * 0.10 + 1)))  # 10 %
        num_max_watch = max(1, self.config.periodic_workers - 5)  # leave at leas few threads available
        logger.info('Max watch: %s, Max recon: %s, Max IPS: %s, Max API: %s'
                    % (num_max_watch, num_max_recon, num_max_ips, num_max_api))

        # semaphore array init
        self.watcher_job_semaphores = collections.defaultdict(lambda: StatSemaphore(num_max_others))
        self.watcher_job_semaphores[JobTypes.TARGET] = StatSemaphore(num_max_watch)
        self.watcher_job_semaphores[JobTypes.SUB] = StatSemaphore(num_max_recon)
        self.watcher_job_semaphores[JobTypes.IP_SCAN] = StatSemaphore(num_max_ips)
        self.watcher_job_semaphores[JobTypes.API_PROC] = StatSemaphore(num_max_api)

        # periodic worker start
        for worker_idx in range(self.config.periodic_workers):
            t = threading.Thread(target=self.periodic_worker_main, args=(worker_idx,))
            self.watcher_workers.append(t)
            t.setDaemon(True)
            t.start()

    def periodic_feeder_main(self):
        """
        Main thread feeding periodic scan job queue from database - according to the records.
        :return:
        """
        if self.args.no_jobs:
            return

        while self.is_running():
            ctime = time.time()

            # trigger if last scan was too old / queue is empty / on event from the interface
            scan_now = False
            if self.watch_last_db_scan + self.watch_db_scan_period <= ctime:
                scan_now = True

            if not scan_now and self.watch_last_db_scan + 2 <= ctime and self.periodic_queue_should_add_new():
                scan_now = True

            if not scan_now:
                time.sleep(0.5)
                continue

            # get the new session
            try:
                self.periodic_feeder()

            except Exception as e:
                logger.error('Exception in processing job %s' % (e, ))
                self.trace_logger.log(e)

            finally:
                self.watch_last_db_scan = ctime

        logger.info('Periodic feeder terminated')

    def periodic_feeder(self):
        """
        Feeder loop body
        :return:
        """
        if self.periodic_queue_is_full():
            return

        s = self.db.get_session()

        try:
            if not self.config.monitor_disabled:
                self._periodic_feeder_watch(s)
                self._periodic_feeder_recon(s)
                self._periodic_feeder_ips(s)

            for server_mod in self.modules:
                server_mod.periodic_feeder(s)

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def _periodic_feeder_watch(self, s):
        """
        Load watcher jobs
        :param s:
        :return:
        """
        if self.periodic_queue_is_full():
            return

        try:
            min_scan_margin = self.min_scan_margin()
            query = self.load_active_watch_targets(s, last_scan_margin=min_scan_margin)
            iterator = query.yield_per(100)
            for x in iterator:
                watch_target, min_periodicity, watch_service = x

                if self.periodic_queue_is_full():
                    return

                job = PeriodicJob(target=watch_target, periodicity=min_periodicity, watch_service=watch_service)
                self.periodic_add_job(job)

        except QFull:
            logger.debug('Queue full')
            return

        except Exception as e:
            s.rollback()
            logger.error('Exception loading watch jobs %s' % e)
            self.trace_logger.log(e)
            raise

    def _periodic_feeder_recon(self, s):
        """
        Load watcher jobs - recon jobs
        :param s:
        :return:
        """
        if self.periodic_queue_is_full(JobTypes.SUB):
            return

        try:
            min_scan_margin = int(self.delta_wildcard.total_seconds())
            query = self.load_active_recon_targets(s, last_scan_margin=min_scan_margin)
            iterator = query.yield_per(100)
            for x in iterator:
                watch_target, min_periodicity = x

                if self.periodic_queue_is_full(JobTypes.SUB):
                    return

                # TODO: analyze if this job should be processed or results are recent, no refresh is needed
                job = PeriodicReconJob(target=watch_target, periodicity=min_periodicity)
                self.periodic_add_job(job)

        except QFull:
            logger.debug('Queue full')
            return

        except Exception as e:
            s.rollback()
            logger.error('Exception loading watch jobs %s' % e)
            self.trace_logger.log(e)
            raise

    def _periodic_feeder_ips(self, s):
        """
        Load watcher jobs - ip scanning jobs
        :param s:
        :return:
        """
        if self.periodic_queue_is_full(JobTypes.IP_SCAN):
            return

        try:
            min_scan_margin = int(self.delta_wildcard.total_seconds())
            query = self.load_active_ip_scan_targets(s, last_scan_margin=min_scan_margin)
            iterator = query.yield_per(100)
            for x in iterator:
                watch_target, min_periodicity = x

                if self.periodic_queue_is_full(JobTypes.IP_SCAN):
                    return

                # TODO: analyze if this job should be processed or results are recent, no refresh is needed
                job = PeriodicIpScanJob(target=watch_target, periodicity=min_periodicity)
                self.periodic_add_job(job)

        except QFull:
            logger.debug('Queue full')
            return

        except Exception as e:
            s.rollback()
            logger.error('Exception loading watch jobs %s' % e)
            self.trace_logger.log(e)
            raise

    def periodic_add_job(self, job):
        """
        Adds job to the queue
        :param job:
        :return:
        """
        with self.watcher_db_lock:
            # Ignore jobs currently in the progress.
            if job.key() in self.watcher_db_cur_jobs:
                return

            self.watcher_db_cur_jobs[job.key()] = job
            self.watcher_job_queue.put(job)
            logger.debug('Job generated: %s, qsize: %s, sems: %s'
                         % (str(job), self.watcher_job_queue.qsize(), self.periodic_semaphores()))

    def periodic_semaphores(self):
        """
        Simple state dump on busy threads, returns string
        :return:
        """
        sems = self.watcher_job_semaphores
        return '|'.join(['%s=%s' % (k, sems[k].countinv()) for k in sems])

    def periodic_worker_main(self, idx):
        """
        Main periodic job worker
        :param idx:
        :return:
        """
        self.local_data.idx = idx
        logger.info('Periodic Scanner Worker %02d started' % idx)

        while self.is_running():
            job = None
            try:
                job = self.watcher_job_queue.get(True, timeout=1.0)
            except QEmpty:
                time.sleep(0.1)
                continue

            try:
                # Process job in try-catch so it does not break worker
                # logger.debug('[%02d] Processing job' % (idx,))
                self.periodic_process_job(job)

            except Exception as e:
                logger.error('Exception in processing watch job %s: %s' % (e, job))
                self.trace_logger.log(e)

            finally:
                self.watcher_job_queue.task_done()

        logger.info('Periodic Worker %02d terminated' % idx)

    def periodic_process_job(self, job):
        """
        Processes periodic job - wrapper
        :param job:
        :type job: BaseJob
        :return:
        """
        sem = self.watcher_job_semaphores[job.type]  # type: StatSemaphore
        sem_acquired = False
        try:
            with self.watcher_db_lock:
                self.watcher_db_processing[job.key()] = job

            sem_acquired = sem.acquire(False)  # simple job type scheduling
            if sem_acquired:
                job.reset_later()
                if job.type == JobTypes.TARGET:
                    self.periodic_process_job_body(job)
                elif job.type == JobTypes.SUB:
                    self.periodic_process_recon_job_body(job)
                elif job.type == JobTypes.IP_SCAN:
                    self.periodic_process_ips_job_body(job)
                else:
                    consumed = self.periodic_feed_job_module(job)
                    if not consumed:
                        raise ValueError('Unrecognized job type: %s' % job.type)

        except Exception as e:
            logger.error('Exception in processing watcher job %s' % (e,))
            self.trace_logger.log(e)

        finally:
            remove_job = True
            readd_job = False
            if sem_acquired:
                sem.release()

            # Later? re-enqueue
            if not sem_acquired:
                remove_job = False
                readd_job = True
                job.inclater()

            # if job is success update db last scan value
            elif job.success_scan:
                self.periodic_update_last_scan(job)

            # if retry under threshold, add again to the queue
            elif job.attempts <= 3:
                readd_job = True
                remove_job = False

            # The job has expired.
            # TODO: make sure job does not return quickly by DB load - add backoff / num of fails / last fail
            # remove from processing caches so it can be picked up again later.
            # i.e. remove lock on this item
            if remove_job:
                self.periodic_update_last_scan(job)  # job failed even after many attempts
                with self.watcher_db_lock:
                    del self.watcher_db_cur_jobs[job.key()]

            with self.watcher_db_lock:
                del self.watcher_db_processing[job.key()]

            if readd_job:
                self.watcher_job_queue.put(job)

    def periodic_feed_job_module(self, job):
        """
        Feeds job to the modules
        :param job:
        :return: True if job was consumed
        """
        consumed = False
        for server_mod in self.modules:
            consumed |= server_mod.process_periodic_job(job)
            if consumed:
                break
        return consumed

    def periodic_update_last_scan(self, job):
        """
        Updates last scan time for the job
        :param job:
        :type job: BaseJob
        :return:
        """
        if job.type == JobTypes.TARGET:
            self._periodic_update_last_scan_watch(job)
        elif job.type == JobTypes.SUB:
            self._periodic_update_last_scan_recon(job)
        elif job.type == JobTypes.IP_SCAN:
            self._periodic_update_last_ip_scan(job)
        elif not self.periodic_update_last_scan_module(job):
            raise ValueError('Unrecognized job type')

    def periodic_update_last_scan_module(self, job):
        """
        Scan jobs for last scan update for the job.
        Feeds job to the module. Returns False if job was not consumed by any module.
        :param job:
        :return:
        """
        consumed = False
        for server_mod in self.modules:
            consumed |= server_mod.periodic_job_update_last_scan(job)
            if consumed:
                break
        return consumed

    def _periodic_update_last_scan_watch(self, job):
        """
        Updates watcher job specifically
        :param job:
        :return:
        """
        s = self.db.get_session()
        try:
            stmt = DbWatchTarget.__table__.update()\
                .where(DbWatchTarget.id == job.target.id)\
                .values(last_scan_at=salch.func.now())
            s.execute(stmt)
            s.commit()

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def _periodic_update_last_scan_recon(self, job):
        """
        Updates watcher job specifically
        :param job:
        :return:
        """
        s = self.db.get_session()
        try:
            stmt = DbSubdomainWatchTarget.__table__.update()\
                .where(DbSubdomainWatchTarget.id == job.target.id)\
                .values(last_scan_at=salch.func.now())
            s.execute(stmt)
            s.commit()

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def _periodic_update_last_ip_scan(self, job):
        """
        Updates IP scan job specifically
        :param job:
        :return:
        """
        s = self.db.get_session()
        try:
            stmt = DbIpScanRecord.__table__.update()\
                .where(DbIpScanRecord.id == job.target.id)\
                .values(last_scan_at=salch.func.now())
            s.execute(stmt)
            s.commit()

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def periodic_process_job_body(self, job):
        """
        Watcher job processing - the body
        :param job:
        :type job: PeriodicJob
        :return:
        """
        logger.debug('Processing watcher job: %s, qsize: %s, sems: %s'
                     % (job, self.watcher_job_queue.qsize(), self.periodic_semaphores()))

        s = None
        url = None

        try:
            url = self.urlize(job)
            if not TlsDomainTools.can_connect(url.host):
                raise InvalidHostname('Invalid host name')

            s = self.db.get_session()
            self.periodic_scan_dns(s, job)
            self.periodic_scan_tls(s, job)
            self.periodic_scan_crtsh(s, job)
            self.periodic_scan_whois(s, job)

            job.success_scan = True  # updates last scan record

            # each scan can fail independently. Successful scans remain valid.
            if job.scan_dns.is_failed() \
                    or job.scan_tls.is_failed() \
                    or job.scan_whois.is_failed() \
                    or job.scan_crtsh.is_failed():
                logger.info('Job failed, dns: %s, tls: %s, whois: %s, crtsh: %s'
                            % (job.scan_dns.is_failed(), job.scan_tls.is_failed(),
                               job.scan_whois.is_failed(), job.scan_crtsh.is_failed()))

                job.attempts += 1
                job.success_scan = False
            else:
                job.success_scan = True

        except InvalidHostname as ih:
            logger.debug('Invalid host: %s' % url)
            job.success_scan = True  # TODO: back-off / disable, fatal error

        except Exception as e:
            logger.debug('Exception when processing the watcher job: %s' % e)
            self.trace_logger.log(e)
            job.attempts += 1

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def periodic_process_recon_job_body(self, job):
        """
        Watcher recon job processing - the body
        :param job:
        :type job: PeriodicReconJob
        :return:
        """
        logger.debug('Processing watcher recon job: %s, qsize: %s, sems: %s'
                     % (job, self.watcher_job_queue.qsize(), self.periodic_semaphores()))
        s = None
        url = None

        try:
            url = self.urlize(job)

            if not TlsDomainTools.can_connect(url.host):
                raise InvalidHostname('Invalid host name')

            s = self.db.get_session()
            self.periodic_scan_subdomain(s, job)
            job.success_scan = True  # updates last scan record

            # each scan can fail independently. Successful scans remain valid.
            if job.scan_crtsh_wildcard.is_failed():
                logger.info('Job failed, wildcard: %s' % (job.scan_crtsh_wildcard.is_failed()))
                job.attempts += 1
                job.success_scan = False

            else:
                job.success_scan = True

        except InvalidHostname as ih:
            logger.debug('Invalid host: %s' % url)
            job.success_scan = True  # TODO: back-off / disable, fatal error
            
        except Exception as e:
            logger.debug('Exception when processing the watcher recon job: %s' % e)
            self.trace_logger.log(e)
            job.attempts += 1

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def periodic_scan_dns(self, s, job):
        """
        Periodic DNS scan - determines if the check is required, invokes the check
        :param s:
        :param job:
        :type job: PeriodicJob
        :return:
        """
        job_scan = job.scan_dns  # type: ScanResults
        last_scan = self.load_last_dns_scan_optim(s, job.watch_id())
        if last_scan is not None \
                and last_scan.last_scan_at \
                and last_scan.last_scan_at > self.diff_time(self.delta_dns, rnd=True):
            job_scan.skip(last_scan)
            self.wp_process_dns(s, job, job_scan.aux)
            return  # scan is relevant enough

        try:
            self.wp_scan_dns(s, job, last_scan)

        except Exception as e:
            job_scan.fail()

            logger.error('DNS scan exception: %s' % e)
            self.trace_logger.log(e, custom_msg='DNS scan')

    def periodic_scan_tls(self, s, job):
        """
        Periodic TLS scan - determines if the check is required, invokes the check
        :param s:
        :param job:
        :type job: PeriodicJob
        :return:
        """
        job_scan = job.scan_tls  # type: ScanResults
        job_dns = job.scan_dns  # type: ScanResults

        if job.target.agent_id is not None:
            job_scan.skip()  # TLS scan is agent specific
            return

        if util.is_empty(job.ips):
            job_scan.skip()  # DNS is an important part, if watch cannot be resolved - give up.
            return

        prev_scans = self.load_last_tls_scan_last_dns(s, job.watch_id(), job.ips)
        prev_scans_map = {x.ip_scanned: x for x in prev_scans}

        # repeat
        ips_set = set(job.ips)
        scans_to_repeat = list(ips_set - set([x.ip_scanned for x in prev_scans]))  # not scanned yet
        scans_to_repeat += [x.ip_scanned for x in prev_scans
                            if x.ip_scanned != '-' and x.ip_scanned in ips_set
                            and (not x.last_scan_at or x.last_scan_at <= self.diff_time(self.delta_tls, rnd=True))]

        logger.debug('ips: %s, repeat: %s, url: %s, scan map: %s, '
                     % (job.ips, scans_to_repeat, self.urlize(job), prev_scans_map))

        if len(scans_to_repeat) == 0:
            job_scan.skip(prev_scans_map)
            return  # scan is relevant enough

        try:
            for cur_ip in scans_to_repeat:
                self.wp_scan_tls(s, job, prev_scans_map, ip=cur_ip)
            job_scan.ok()

        except Exception as e:
            job_scan.fail()

            logger.error('TLS scan exception: %s' % e)
            self.trace_logger.log(e, custom_msg='TLS scan')

    def periodic_scan_crtsh(self, s, job):
        """
        Periodic CRTsh scan - determines if the check is required, invokes the check
        :param s:
        :param job:
        :type job: PeriodicJob
        :return:
        """
        job_scan = job.scan_crtsh  # type: ScanResults

        last_scan = self.load_last_crtsh_scan(s, job.watch_id())
        if last_scan is not None \
                and last_scan.last_scan_at \
                and last_scan.last_scan_at > self.diff_time(self.delta_crtsh, rnd=True):
            job_scan.skip(last_scan)
            return  # scan is relevant enough

        try:
            self.wp_scan_crtsh(s, job, last_scan)

        except Exception as e:
            job_scan.fail()

            logger.error('CRT sh exception: %s' % e)
            self.trace_logger.log(e, custom_msg='CRT sh')

    def periodic_scan_whois(self, s, job):
        """
        Periodic Whois scan - determines if the check is required, invokes the check
        :param s:
        :param job:
        :type job: PeriodicJob
        :return:
        """
        url = self.urlize(job)
        job_scan = job.scan_whois  # type: ScanResults

        if not TlsDomainTools.can_whois(url.host):
            job_scan.skip()
            return  # has IP address only, no whois check

        top_domain = TlsDomainTools.get_top_domain(url.host)
        top_domain, is_new = self.db_manager.load_top_domain(s, top_domain)
        last_scan = self.load_last_whois_scan(s, top_domain) if not is_new else None
        if last_scan is not None \
                and last_scan.last_scan_at \
                and last_scan.last_scan_at > self.diff_time(self.delta_whois, rnd=True):
            job_scan.skip(last_scan)
            return  # scan is relevant enough

        # initiate new whois check
        try:
            self.wp_scan_whois(s=s, job=job, url=url, top_domain=top_domain, last_scan=last_scan)

        except Exception as e:
            job_scan.fail()

            logger.error('Whois exception: %s' % e)
            self.trace_logger.log(e, custom_msg='Whois')

    def periodic_scan_subdomain(self, s, job):
        """
        Periodic CRTsh wildcard scan
        :param s:
        :param job:
        :type job: PeriodicReconJob
        :return:
        """
        job_scan = job.scan_crtsh_wildcard  # type: ScanResults

        # last scan determined by special wildcard query for the watch host
        query, is_new = self.db_manager.get_crtsh_input(s, job.target.scan_host, 2)
        last_scan = self.load_last_crtsh_wildcard_scan(s, watch_id=job.watch_id(), input_id=query.id)
        if last_scan is not None \
                and last_scan.last_scan_at \
                and last_scan.last_scan_at > self.diff_time(self.delta_wildcard, rnd=True):
            job_scan.skip(last_scan)
            return  # scan is relevant enough

        try:
            self.wp_scan_crtsh_wildcard(s, job, last_scan)

        except Exception as e:
            job_scan.fail()

            logger.error('CRT sh wildcard exception: %s' % e)
            self.trace_logger.log(e, custom_msg='CRT sh wildcard')

    def periodic_process_ips_job_body(self, job):
        """
        IP scanner job processing - the body
        :param job:
        :type job: PeriodicIpScanJob
        :return:
        """
        logger.debug('Processing IP scan recon job: %s, qsize: %s, sems: %s'
                     % (job, self.watcher_job_queue.qsize(), self.periodic_semaphores()))
        s = None
        url = None

        try:
            if not TlsDomainTools.is_valid_ipv4_address(job.target.ip_beg) or \
                    not TlsDomainTools.is_valid_ipv4_address(job.target.ip_end):
                raise InvalidHostname('Invalid host name')

            if TlsDomainTools.ip_range(job.target.ip_beg, job.target.ip_end) > 2**14:
                raise InvalidHostname('IP range is too big')

            s = self.db.get_session()

            self.periodic_scan_ip_range(s, job)
            job.success_scan = True  # updates last scan record

            # each scan can fail independently. Successful scans remain valid.
            if job.scan_ip_scan.is_failed():
                logger.info('Job failed, wildcard: %s' % (job.scan_ip_scan.is_failed()))
                job.attempts += 1
                job.success_scan = False

            else:
                job.success_scan = True

        except InvalidHostname as ih:
            logger.debug('Invalid host: %s' % url)
            job.success_scan = True  # TODO: back-off / disable, fatal error

        except Exception as e:
            logger.debug('Exception when processing the IP scan job: %s' % e)
            self.trace_logger.log(e)
            job.attempts += 1

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

    def periodic_scan_ip_range(self, s, job):
        """
        Scanning IP range, looking for services
        :param s:
        :param PeriodicIpScanJob job:
        :return:
        """
        job_scan = job.scan_ip_scan  # type: ScanResults

        # last scan determined by special wildcard query for the watch host
        last_scan = self.load_last_ip_scan_result_cached(s, record_id=job.record_id())
        if last_scan is not None \
                and last_scan.last_scan_at \
                and last_scan.last_scan_at > self.diff_time(self.delta_ip_scan, rnd=True):
            job_scan.skip(last_scan)
            return  # scan is relevant enough

        try:
            self.wp_scan_ip_scan(s, job, last_scan)
            pass

        except Exception as e:
            job_scan.fail()

            logger.error('IP scanning exception: %s' % e)
            self.trace_logger.log(e, custom_msg='IP scanning')

    #
    # Scan bodies
    #

    def _create_job_spec(self, job):
        """
        Builds job defs for scan - like job spec coming from frontend
        :param job:
        :type job: PeriodicJob
        :return:
        """
        data = collections.OrderedDict()
        data['uuid'] = None
        data['state'] = 'init'
        data['scan_type'] = 'planner'
        data['user_id'] = None

        url = self.urlize(job)
        data['scan_scheme'] = url.scheme
        data['scan_host'] = url.host
        data['scan_port'] = url.port
        data['scan_url'] = url

        data = self.augment_redis_scan_job(data=data)
        return data

    def wp_scan_dns(self, s, job, last_scan):
        """
        Watcher DNS scan - body
        :param s:
        :param job:
        :type job: PeriodicJob
        :param last_scan:
        :type last_scan: DbDnsResolve
        :return:
        """
        job_scan = job.scan_dns  # type: ScanResults
        job_spec = self._create_job_spec(job)
        url = self.urlize(job)

        if TlsDomainTools.is_ip(url.host):
            job.primary_ip = url.host
            job.ips = [url.host]
            job_scan.skip()
            return

        if job.target.manual_dns:
            job_scan.skip()
            return

        if not TlsDomainTools.can_whois(url.host):
            logger.debug('Domain %s not eligible to DNS scan' % url.host)
            job_scan.skip()
            return

        cur_scan, dns_entries = self.scan_dns(s=s, job_data=job_spec, query=url.host, job_db=None, store_to_db=False)
        if cur_scan is None:
            job_scan.fail()
            return

        is_same_as_before = self.diff_scan_dns(cur_scan, last_scan)
        if is_same_as_before:
            last_scan.last_scan_at = salch.func.now()
            last_scan.num_scans += 1
            job_scan.aux = last_scan

        else:
            cur_scan.watch_id = job.target.id
            cur_scan.num_scans = 1
            cur_scan.updated_at = salch.func.now()
            cur_scan.last_scan_at = salch.func.now()
            s.add(cur_scan)
            s.flush()

            for entry in dns_entries:
                entry.scan_id = cur_scan.id
                s.add(entry)
            s.flush()
            s.commit()

            # update cached last dns scan id
            self.db_manager.update_last_dns_scan_id(s, cur_scan)
            self.db_manager.update_watch_ip_type(s, job.target)
            
            job_scan.aux = cur_scan

        s.commit()

        # Store scan history
        hist = DbScanHistory()
        hist.watch_id = job.target.id
        hist.scan_type = 4  # dns scan
        hist.scan_code = 0
        hist.created_at = salch.func.now()
        s.add(hist)
        s.commit()

        # TODO: store gap if there is one
        # - compare last scan with the SLA periodicity. multiple IP addressess make it complicated...
        self.wp_process_dns(s, job, job_scan.aux)

        # Eventing
        if not is_same_as_before:
            self.on_new_scan(s, old_scan=last_scan, new_scan=cur_scan, job=job)

        # finished with success
        job_scan.ok()

    def wp_process_dns(self, s, job, last_scan):
        """
        Processes DNS scan, sets primary IP address
        :param s:
        :param job:
        :type job: ScanJob
        :param last_scan:
        :type last_scan: DbDnsResolve
        :return:
        """
        if last_scan and last_scan.dns_res and len(last_scan.dns_res) > 0:
            domains = sorted(last_scan.dns_res)
            job.primary_ip = domains[0][1]
            job.ips = [x[1] for x in last_scan.dns_res]
        else:
            job.primary_ip = None

    def wp_scan_tls(self, s, job, scan_list, ip=None):
        """
        Watcher TLS scan - body
        :param s:
        :param job:
        :type job: PeriodicJob
        :param scan_list:
        :param ip:
        :return:
        """
        job_scan = job.scan_tls  # type: ScanResults
        job_spec = self._create_job_spec(job)
        url = self.urlize(job)

        if job.service is not None:
            job_spec['scan_sni'] = job.service.service_name
        elif TlsDomainTools.can_whois(url.host):
            job_spec['scan_sni'] = url.host

        # For now - scan only first IP address in the lexi ordering
        job_spec['dns_ok'] = True
        if ip:
            job_spec['scan_host'] = ip
        elif job.primary_ip:
            job_spec['scan_host'] = job.primary_ip
        else:
            job_scan.skip(scan_list)  # skip TLS handshake check totally if DNS is not valid
            return False

        # Cname from DNS scan
        if job.scan_dns and job.scan_dns.aux and job.scan_dns.aux.cname:
            job_spec['cname'] = job.scan_dns.aux.cname

        handshake_res, db_scan = self.scan_handshake(s, job_spec, url.host, None, store_job=False)
        if handshake_res is None:
            return False

        last_scan = util.defvalkey(scan_list, db_scan.ip_scanned, None)

        # Compare with last result, store if new one or update the old one
        is_same_as_before = self.diff_scan_tls(db_scan, last_scan)
        if is_same_as_before:
            last_scan.last_scan_at = salch.func.now()
            last_scan.num_scans += 1
            last_scan = s.merge(last_scan)
            s.commit()

        else:
            db_scan.watch_id = job.target.id
            db_scan.num_scans = 1
            db_scan.last_scan_at = salch.func.now()
            db_scan.updated_at = salch.func.now()
            s.add(db_scan)
            s.commit()
            logger.info('TLS scan is different, lastscan: %s for %s' % (last_scan, db_scan.ip_scanned))

            # update last scan cache
            ResultModelUpdater.update_cache(s, db_scan)

        # Store scan history
        hist = DbScanHistory()
        hist.watch_id = job.target.id
        hist.scan_type = 1
        hist.scan_code = 0
        hist.created_at = salch.func.now()
        s.add(hist)
        s.commit()

        # TODO: store gap if there is one
        # - compare last scan with the SLA periodicity. multiple IP addressess make it complicated...

        # Eventing
        if not is_same_as_before:
            self.on_new_scan(s, old_scan=last_scan, new_scan=db_scan, job=job)

        return True

    def wp_scan_crtsh(self, s, job, last_scan):
        """
        Watcher crt.sh scan - body
        :param s:
        :param job:
        :type job: PeriodicJob
        :param last_scan:
        :type last_scan: DbCrtShQuery
        :return:
        """
        job_scan = job.scan_crtsh  # type: ScanResults
        job_spec = self._create_job_spec(job)
        url = self.urlize(job)

        crtsh_query_db, sub_res_list = self.scan_crt_sh(
            s=s, job_data=job_spec, query=url.host, job_db=None, store_to_db=False)
        if crtsh_query_db is None:
            job_scan.fail()
            return

        is_same_as_before = self.diff_scan_crtsh(crtsh_query_db, last_scan)
        if is_same_as_before:
            last_scan.last_scan_at = salch.func.now()
            last_scan.num_scans += 1

            if last_scan.input_id is None:  # migration to input ids
                last_scan.input_id = crtsh_query_db.input_id
            last_scan = s.merge(last_scan)
            s.commit()

        else:
            crtsh_query_db.watch_id = job.target.id
            crtsh_query_db.num_scans = 1
            crtsh_query_db.updated_at = salch.func.now()
            crtsh_query_db.last_scan_at = salch.func.now()
            s.add(crtsh_query_db)
            s.commit()

            # update last scan cache
            ResultModelUpdater.update_cache(s, crtsh_query_db)

        # Store scan history
        hist = DbScanHistory()
        hist.watch_id = job.target.id
        hist.scan_type = 2  # crtsh scan
        hist.scan_code = 0
        hist.created_at = salch.func.now()
        s.add(hist)
        s.commit()

        # TODO: store gap if there is one
        # - compare last scan with the SLA periodicity. multiple IP addressess make it complicated...

        # Eventing
        if not is_same_as_before:
            self.on_new_scan(s, old_scan=last_scan, new_scan=crtsh_query_db, job=job)

        # finished with success
        job_scan.ok()

    def wp_scan_crtsh_wildcard(self, s, job, last_scan):
        """
        Watcher crt.sh wildcard scan - body
        :param s:
        :param job:
        :type job: PeriodicReconJob
        :param last_scan:
        :type last_scan: DbCrtShQuery
        :return:
        """
        job_scan = job.scan_crtsh_wildcard  # type: ScanResults
        job_spec = self._create_job_spec(job)

        # top domain set?
        self.fix_sub_watch_target_domain(s, job.target)

        # blacklisting check
        if self.is_blacklisted(job.target.scan_host) is not None:
            logger.debug('Domain blacklisted: %s' % job.target.scan_host)
            job_scan.ok()
            return

        # define wildcard & base scan input
        query, is_new = self.db_manager.get_crtsh_input(s, job.target.scan_host, 2)
        query_base, is_new = self.db_manager.get_crtsh_input(s, job.target.scan_host, 0)

        # perform crtsh queries, result management
        is_same_as_before_sc, crtsh_query_db, sub_res_list, crtsh_query_db_base, sub_res_list_base = \
            self.wp_scan_wildcard_query(s, query=query, query_base=query_base,
                                        job=job, job_spec=job_spec, last_scan=last_scan)

        # load previous cached result, may be empty.
        last_cache_res = self.load_last_subs_result(s, watch_id=job.watch_id())
        is_same_as_before = is_same_as_before_sc and last_cache_res is not None
        db_sub = None

        # new result - store new subdomain data, invalidate old results
        if not is_same_as_before:
            # - extract domains to the result cache....
            # - load previously saved certs, not loaded now, from db
            # TODO: load previous result, just add altnames added in new certificates.
            sub_lists = list(sub_res_list) + list(sub_res_list_base)
            certs_to_load = list(set([x.crt_id for x in sub_lists
                                      if x is not None and x.crt_sh_id is not None and x.cert_db is None]))
            certs_loaded = list(self.cert_manager.cert_load_by_id(s, certs_to_load).values())
            certs_downloaded = [x.cert_db for x in sub_lists
                                if x is not None and x.cert_db is not None]

            all_alt_names = set()
            for cert in (certs_loaded + certs_downloaded):  # type: Certificate
                for alt in cert.all_names:
                    all_alt_names.add(util.lower(alt))

            # - filter out alt names not ending on the target
            suffix = '.%s' % query.iquery
            suffix_alts = set()
            for alt in all_alt_names:
                if alt.endswith(suffix) or alt == query.iquery:
                    suffix_alts.add(alt)

            # Result
            db_sub = DbSubdomainResultCache()
            db_sub.watch_id = job.watch_id()
            db_sub.created_at = salch.func.now()
            db_sub.updated_at = salch.func.now()
            db_sub.last_scan_at = salch.func.now()
            db_sub.last_scan_idx = crtsh_query_db.newest_cert_sh_id
            db_sub.num_scans = 1
            db_sub.scan_type = 1  # crtsh
            db_sub.trans_result = sorted(list(suffix_alts))
            db_sub.result_size = len(db_sub.trans_result)
            db_sub.result = json.dumps(db_sub.trans_result)

            mm = DbSubdomainResultCache
            is_same, db_sub_new, last_scan = \
                ResultModelUpdater.insert_or_update(s, [mm.watch_id, mm.scan_type], [mm.result], db_sub)
            s.commit()

            # update last scan cache
            if not is_same:
                ResultModelUpdater.update_cache(s, db_sub_new)

            # Subdomains insert / update
            self.subs_sync_records(s, job, db_sub_new)

            # Add new watcher targets automatically - depends on the assoc model, if enabled
            self.auto_fill_new_watches(s, job, db_sub)  # returns is_same, obj, last_scan

        # Store scan history
        # hist = DbScanHistory()
        # hist.watch_id = job.target.id
        # hist.scan_type = 20  # crtsh w scan
        # hist.scan_code = 0
        # hist.created_at = salch.func.now()
        # s.add(hist)  # TODO: constraint, cannot insert here
        s.commit()

        # TODO: store gap if there is one
        # - compare last scan with the SLA periodicity. multiple IP addressess make it complicated...

        # Eventing
        if not is_same_as_before:
            self.on_new_scan(s, old_scan=last_cache_res, new_scan=db_sub, job=job)

        # finished with success
        job_scan.ok()

    def wp_scan_whois(self, s, job, url, top_domain, last_scan):
        """
        Watcher whois scan - body
        :param s:
        :param job:
        :type job: PeriodicJob
        :param url:
        :param top_domain:
        :type top_domain: DbBaseDomain
        :param last_scan:
        :type last_scan: DbWhoisCheck
        :return:
        """
        job_scan = job.scan_whois  # type: ScanResults
        job_spec = self._create_job_spec(job)
        url = self.urlize(job)

        scan_db = self.scan_whois(s=s, job_data=job_spec, query=top_domain, job_db=None, store_to_db=False)
        if scan_db.status == 3:  # too fast
            job_scan.fail()
            return

        # Compare with last result, store if new one or update the old one
        is_same_as_before = self.diff_scan_whois(scan_db, last_scan)
        if is_same_as_before:
            last_scan.last_scan_at = salch.func.now()
            last_scan.num_scans += 1
            last_scan = s.merge(last_scan)
        else:
            scan_db.watch_id = job.target.id
            scan_db.num_scans = 1
            scan_db.updated_at = salch.func.now()
            scan_db.last_scan_at = salch.func.now()
            s.add(scan_db)

        # top domain assoc
        if job.target.top_domain_id is None or job.target.top_domain_id != top_domain.id:
            job.target.top_domain_id = top_domain.id
            s.merge(job.target)
        s.commit()

        # update last scan cache
        if not is_same_as_before:
            ResultModelUpdater.update_cache(s, scan_db)

        # Store scan history
        hist = DbScanHistory()
        hist.watch_id = job.target.id
        hist.scan_type = 3  # whois
        hist.scan_code = 0
        hist.created_at = salch.func.now()
        s.add(hist)
        s.commit()

        # TODO: store gap if there is one
        # - compare last scan with the SLA periodicity. multiple IP addressess make it complicated...

        # Eventing
        if not is_same_as_before:
            self.on_new_scan(s, old_scan=last_scan, new_scan=scan_db, job=job)

        job_scan.ok()

    def wp_scan_wildcard_query(self, s, query, query_base, job, job_spec, last_scan):
        """
        Performs CRTSH queries & compares with previous results, updates the DB info.
        :param s:
        :param query:
        :param query_base:
        :param job:
        :param job_spec:
        :param last_scan:
        :return:
        """

        # crtsh search for base record
        crtsh_query_db_base, sub_res_list_base = self.scan_crt_sh(
            s=s, job_data=job_spec, query=query_base, job_db=None, store_to_db=False)

        # load previous input id scan - for change detection
        last_scan_base = self.load_last_crtsh_wildcard_scan(s, watch_id=job.target.id, input_id=query_base.id)
        is_same_as_before_base = self.diff_scan_crtsh_wildcard(crtsh_query_db_base, last_scan_base)

        if is_same_as_before_base:
            last_scan_base.last_scan_at = salch.func.now()
            last_scan_base.num_scans += 1
            if last_scan_base.input_id is None:  # migration to input ids
                last_scan_base.input_id = crtsh_query_db_base.input_id
            last_scan_base = s.merge(last_scan_base)

        else:
            crtsh_query_db_base.sub_watch_id = job.target.id
            crtsh_query_db_base.num_scans = 1
            crtsh_query_db_base.updated_at = salch.func.now()
            crtsh_query_db_base.last_scan_at = salch.func.now()
            s.add(crtsh_query_db_base)
        s.commit()

        # WILDCARD
        # crtsh search for wildcard
        crtsh_query_db, sub_res_list = self.scan_crt_sh(
            s=s, job_data=job_spec, query=query, job_db=None, store_to_db=False)

        is_same_as_before = self.diff_scan_crtsh_wildcard(crtsh_query_db, last_scan)
        if is_same_as_before:
            last_scan.last_scan_at = salch.func.now()
            last_scan.num_scans += 1
            if last_scan.input_id is None:  # migration to input ids
                last_scan.input_id = crtsh_query_db.input_id
            last_scan = s.merge(last_scan)

        else:
            crtsh_query_db.sub_watch_id = job.target.id
            crtsh_query_db.num_scans = 1
            crtsh_query_db.updated_at = salch.func.now()
            crtsh_query_db.last_scan_at = salch.func.now()
            s.add(crtsh_query_db)

        s.commit()
        return is_same_as_before_base and is_same_as_before, \
               crtsh_query_db, sub_res_list, crtsh_query_db_base, sub_res_list_base

    def wp_scan_ip_scan(self, s, job, last_scan):
        """
        IP scan scan - body
        :param s:
        :param job:
        :type job: PeriodicIpScanJob
        :param last_scan:
        :type last_scan: DbIpScanRecord
        :return:
        """
        job_scan = job.scan_ip_scan  # type: ScanResults
        job_spec = self._create_job_spec(job)
        job_spec['dns_ok'] = True
        job_spec['scan_sni'] = job.target.service_name
        job_spec['scan_host'] = job.target.service_name
        job_spec['scan_port'] = job.target.service_port

        # service id filled in?
        refresh_target = False
        if job.target.service_id is None:
            db_svc, db_svc_new = self.db_manager.load_watch_service(s, job.target.service_name)
            if db_svc is not None:
                job.target.service_id = db_svc.id
                refresh_target = True

        if job.target.ip_beg_int is None:
            job.target.ip_beg_int = TlsDomainTools.ip_to_int(job.target.ip_beg)
            job.target.ip_end_int = TlsDomainTools.ip_to_int(job.target.ip_end)
            refresh_target = True
            
        if refresh_target:
            s.merge(job.target)
            s.flush()

        # perform crtsh queries, result management
        scan_db = self.wp_scan_ip_body(s, job, job_spec, last_scan)

        # Compare with last result, store if new one or update the old one
        is_same_as_before = self.diff_scan_ip_scan(scan_db, last_scan)
        if is_same_as_before:
            last_scan.last_scan_at = salch.func.now()
            last_scan.num_scans += 1
            last_scan = s.merge(last_scan)

        else:
            logger.info('New IP scan results, scan rec id: %s, svc: %s, ips found: %s'
                        % (job.target.id, job.target.service_name, util.json_dumps(scan_db.trans_ips_found)))

            s.add(scan_db)
            s.commit()

            # Add new watcher targets automatically - depends on the assoc model, if enabled
            self.auto_fill_ip_watches(s, job, scan_db)  # returns is_same, obj, last_scan

            target = s.merge(job.target)
            target.last_result_id = scan_db.id

            ResultModelUpdater.update_cache(s, scan_db)
            s.commit()

        s.commit()

        # TODO: store gap if there is one
        # - compare last scan with the SLA periodicity. multiple IP addressess make it complicated...

        # Eventing
        if not is_same_as_before:
            self.on_new_scan(s, old_scan=last_scan, new_scan=scan_db, job=job)

        # finished with success
        job_scan.ok()

    def wp_scan_ip_body(self, s, job, job_spec, last_scan):
        """
        The scanning body
        :param s:
        :param job:
        :type job: PeriodicIpScanJob
        :param job_spec:
        :param last_scan:
        :type last_scan: DbIpScanResult
        :return:
        """
        rec = job.target
        ip_int_beg = TlsDomainTools.ip_to_int(job.target.ip_beg)
        ip_int_end = TlsDomainTools.ip_to_int(job.target.ip_end)
        iter = TlsDomainTools.iter_ips(ip_start_int=ip_int_beg, ip_stop_int=ip_int_end)

        time_start = time.time()
        live_ips_ids = []
        valid_ips_ids = []
        valid_ips = []
        for ip in iter:
            # Server termination - abort job, will be performed all over again next time
            if not self.is_running():
                raise ServerShuttingDown('IP scanning aborted')

            # TODO: easy parallelization, threads, input = job_spec, output = handshake, db_scan
            # Greater chunks are needed, 1 IP per thread is not efficient, take at least 10 per thread.
            job_spec['scan_ip'] = ip
            job_spec['sysparams']['timeout'] = 5
            job_spec['sysparams']['retry'] = 2
            handshake_res, db_scan = \
                self.scan_handshake(s, job_spec, None, None, store_job=False,
                                    do_connect_analysis=False, do_process_certificates=False)

            if db_scan.err_code:
                continue

            db_ip, db_ip_new = self.db_manager.load_ip_address(s, ip)
            live_ips_ids.append(db_ip.id)

            if db_scan.valid_hostname:
                valid_ips.append(ip)
                valid_ips_ids.append(db_ip.id)

        live_ips_ids.sort()
        valid_ips.sort()
        valid_ips_ids.sort()

        res = DbIpScanResult()
        res.ip_scan_record_id = rec.id
        res.created_at = salch.func.now()
        res.updated_at = salch.func.now()
        res.last_scan_at = salch.func.now()
        res.finished_at = salch.func.now()

        res.duration = time.time() - time_start
        res.num_ips_alive = len(live_ips_ids)
        res.num_ips_found = len(valid_ips)
        res.ips_alive_ids = json.dumps(live_ips_ids)
        res.ips_found = json.dumps(valid_ips)
        res.ips_found_ids = json.dumps(valid_ips_ids)
        res.trans_ips_alive_ids = live_ips_ids
        res.trans_ips_found = valid_ips
        res.trans_ips_found_ids = valid_ips_ids

        return res

    #
    # Scan Results
    #

    def _res_compare_cols_tls(self):
        """
        Returns list of columns for the result.
        When comparing two different results, these cols should be taken into account.
        :return:
        """
        m = DbHandshakeScanJob  # model, alias
        cols = [
            m.ip_scanned, m.tls_ver, m.status, m.err_code, m.results, m.certs_ids, m.cert_id_leaf,
            m.valid_path, m.valid_hostname, m.err_validity, m.err_many_leafs,
            m.req_https_result, m.follow_http_result, m.follow_https_result,
            ColTransformWrapper(m.follow_http_url, TlsDomainTools.strip_query),
            ColTransformWrapper(m.follow_https_url, TlsDomainTools.strip_query),
            m.hsts_present, m.hsts_max_age, m.hsts_include_subdomains, m.hsts_preload,
            m.pinning_present, m.pinning_report_only, m.pinning_pins,
            m.ip_scanned_reverse, m.cdn_cname, m.cdn_headers, m.cdn_reverse]
        return cols

    def diff_scan_tls(self, cur_scan, last_scan):
        """
        Checks the previous and current scan for significant differences.
        :param cur_scan:
        :type cur_scan: DbHandshakeScanJob
        :param last_scan:
        :type last_scan: DbHandshakeScanJob
        :return:
        """
        # Uses tuple comparison for now. Later it could do comparison by defining
        # columns sensitive for a change dbutil.DbHandshakeScanJob.__table__.columns and getattr(model, col).
        t1, t2 = DbHelper.models_tuples(cur_scan, last_scan, self._res_compare_cols_tls())
        for i in range(len(t1)):
            if t1 and t2 and t1[i] != t2[i]:
                logger.debug('Diff: %s, %s != %s col %s' % (i, t1[i], t2[i], self._res_compare_cols_tls()[i]))
        return t1 == t2

    def _res_compare_cols_crtsh(self):
        """
        Returns list of columns for the result.
        When comparing two different results, these cols should be taken into account.
        :return:
        """
        m = DbCrtShQuery
        return [m.status, m.results, m.certs_ids]

    def diff_scan_crtsh(self, cur_scan, last_scan):
        """
        Checks the previous and current scan for significant differences.
        :param cur_scan:
        :type cur_scan: DbCrtShQuery
        :param last_scan:
        :type last_scan: DbCrtShQuery
        :return:
        """
        return DbHelper.models_tuples_compare(cur_scan, last_scan, self._res_compare_cols_crtsh())

    def _res_compare_cols_whois(self):
        """
        Returns list of columns for the result.
        When comparing two different results, these cols should be taken into account.
        :return:
        """
        m = DbWhoisCheck
        return [m.status, m.registrant_cc, m.registrar, m.registered_at, m.expires_at,
                m.rec_updated_at, m.dns, m.aux]

    def diff_scan_whois(self, cur_scan, last_scan):
        """
        Checks the previous and current scan for significant differences.
        :param cur_scan:
        :type cur_scan: DbWhoisCheck
        :param last_scan:
        :type last_scan: DbWhoisCheck
        :return:
        """
        return DbHelper.models_tuples_compare(cur_scan, last_scan, self._res_compare_cols_whois())

    def _res_compare_cols_dns(self):
        """
        Returns list of columns for the result.
        When comparing two different results, these cols should be taken into account.
        :return:
        """
        m = DbDnsResolve
        return [m.status, m.dns, m.cname]

    def diff_scan_dns(self, cur_scan, last_scan):
        """
        Checks the previous and current scan for significant differences.
        :param cur_scan:
        :type cur_scan: DbDnsResolve
        :param last_scan:
        :type last_scan: DbDnsResolve
        :return:
        """
        return DbHelper.models_tuples_compare(cur_scan, last_scan, self._res_compare_cols_dns())

    def _res_compare_cols_crtsh_wildcard(self):
        """
        Returns list of columns for the result.
        When comparing two different results, these cols should be taken into account.
        :return:
        """
        m = DbCrtShQuery
        return [m.status, m.results, m.newest_cert_sh_id, m.certs_ids]

    def diff_scan_crtsh_wildcard(self, cur_scan, last_scan):
        """
        Checks the previous and current scan for significant differences.
        :param cur_scan:
        :type cur_scan: DbCrtShQuery
        :param last_scan:
        :type last_scan: DbCrtShQuery
        :return:
        """
        return DbHelper.models_tuples_compare(cur_scan, last_scan, self._res_compare_cols_crtsh_wildcard())

    def _res_compare_cols_ip_scan(self):
        """
        Returns list of columns for the result.
        When comparing two different results, these cols should be taken into account.
        :return:
        """
        m = DbIpScanResult
        return [m.num_ips_found, m.ips_found]

    def diff_scan_ip_scan(self, cur_scan, last_scan):
        """
        Checks the previous and current scan for significant differences.
        :param cur_scan:
        :type cur_scan: DbIpScanResult
        :param last_scan:
        :type last_scan: DbIpScanResult
        :return:
        """
        return DbHelper.models_tuples_compare(cur_scan, last_scan, self._res_compare_cols_ip_scan())

    #
    # Scan helpers
    #

    def load_last_tls_scan_last_dns(self, s, watch_id=None, ips=None):
        """
        Loads all previous TLS scans performed against the last DNS scan result set.
        Last scan cache for the given watch_id is used to fetch the results.

        :param s:
        :param watch_id:
        :param ips:
        :return:
        :rtype: list[DbHandshakeScanJob]
        """
        if not isinstance(ips, list):
            ips = [ips]
        else:
            ips = list(ips)

        subq = s.query(DbLastScanCache.scan_id)\
            .filter(DbLastScanCache.cache_type==0)\
            .filter(DbLastScanCache.scan_type==DbScanType.TLS)\
            .filter(DbLastScanCache.obj_id==watch_id)\
            .filter(DbLastScanCache.aux_key.in_(ips))\
            .subquery('x')

        return s.query(DbHandshakeScanJob) \
            .join(subq, subq.c.scan_id == DbHandshakeScanJob.id) \
            .all()

    def load_last_tls_scan(self, s, watch_id=None, ip=None):
        """
        Loads the most recent tls handshake scan result for given watch
        target id and optionally the IP address.
        :param s:
        :param watch_id:
        :param ip:
        :return:
        :rtype DbHandshakeScanJob
        """
        if ip is not None:
            q = s.query(DbHandshakeScanJob).filter(DbHandshakeScanJob.watch_id == watch_id)
            q = q.filter(DbHandshakeScanJob.ip_scanned == ip)
            return q.order_by(DbHandshakeScanJob.last_scan_at.desc()).limit(1).first()

        dialect = util.lower(str(s.bind.dialect.name))
        if dialect.startswith('mysql'):

            group_up = dbutil.assign(
                literal_column('group'),
                DbHandshakeScanJob.ip_scanned).label('grpx')

            rank = case([(
                literal_column('@group') != DbHandshakeScanJob.ip_scanned,
                literal_column('@rownum := 1')
            )], else_=literal_column('@rownum := @rownum + 1')
            ).label('rank')

            subq = s.query(DbHandshakeScanJob)\
                .add_column(rank)\
                .add_column(group_up)

            subr = s.query(
                dbutil.assign(literal_column('rownum'),
                              literal_column('0', type_=salch.types.Numeric())).label('rank'),
                dbutil.assign(literal_column('group'),
                              literal_column('-1', type_=salch.types.Numeric())).label('grpx'),
            ).subquery('r')

            subq = subq.join(subr, salch.sql.expression.literal(True))
            subq = subq.filter(DbHandshakeScanJob.watch_id == watch_id)
            subq = subq.filter(DbHandshakeScanJob.ip_scanned != None)
            subq = subq.order_by(DbHandshakeScanJob.ip_scanned, DbHandshakeScanJob.last_scan_at.desc())
            subq = subq.subquery('x')

            qq = s.query(DbHandshakeScanJob)\
                .join(subq, subq.c.id == DbHandshakeScanJob.id)\
                .filter(subq.c.rank <= 1)
            res = qq.all()
            return res

        else:
            row_number_column = salch.func \
                .row_number() \
                .over(partition_by=DbHandshakeScanJob.ip_scanned,
                      order_by=DbHandshakeScanJob.last_scan_at.desc()) \
                .label('row_number')

            query = s.query(DbHandshakeScanJob)
            query = query.add_column(row_number_column)
            query = query.filter(DbHandshakeScanJob.watch_id == watch_id)
            query = query.filter(DbHandshakeScanJob.ip_scanned != None)
            query = query.from_self().filter(row_number_column == 1)
            return query.all()

    def load_last_crtsh_scan(self, s, watch_id=None):
        """
        Loads the latest crtsh scan for the given watch target id
        :param s:
        :param watch_id:
        :return:
        :rtype DbCrtShQuery
        """
        q = s.query(DbCrtShQuery)\
            .filter(DbCrtShQuery.watch_id == watch_id)\
            .filter(DbCrtShQuery.sub_watch_id == None)
        return q.order_by(DbCrtShQuery.last_scan_at.desc()).limit(1).first()

    def load_last_crtsh_wildcard_scan(self, s, watch_id=None, input_id=None):
        """
        Loads the latest crtsh scan for the given watch target id or input_id or both
        :param s:
        :param watch_id:
        :param input_id:
        :return:
        :rtype DbCrtShQuery
        """
        q = s.query(DbCrtShQuery)

        if watch_id is not None:
            q = q.filter(DbCrtShQuery.watch_id == None)\
                 .filter(DbCrtShQuery.sub_watch_id == watch_id)

        if input_id is not None:
            q = q.filter(DbCrtShQuery.input_id == input_id)

        return q.order_by(DbCrtShQuery.last_scan_at.desc()).limit(1).first()

    def load_last_whois_scan(self, s, top_domain):
        """
        Loads the latest Whois scan for the top domain
        :param s:
        :param top_domain:
        :return:
        :rtype DbWhoisCheck
        """
        if not isinstance(top_domain, DbBaseDomain):
            top_domain, is_new = self.db_manager.load_top_domain(s, top_domain)
            if is_new:
                return None  # non-existing top domain, no result then

        q = s.query(DbWhoisCheck).filter(DbWhoisCheck.domain_id == top_domain.id)
        return q.order_by(DbWhoisCheck.last_scan_at.desc()).limit(1).first()

    def load_last_dns_scan(self, s, watch_id=None):
        """
        Loads the latest DNS scan
        :param s:
        :param watch_id:
        :return:
        :rtype DbDnsResolve
        """
        if watch_id is None:
            return None
        q = s.query(DbDnsResolve).filter(DbDnsResolve.watch_id == watch_id)
        return q.order_by(DbDnsResolve.last_scan_at.desc()).limit(1).first()

    def load_last_dns_scan_optim(self, s, watch_id=None):
        """
        Loads the latest DNS scan - optimized version
        :param s:
        :param watch_id:
        :return:
        :rtype DbDnsResolve
        """
        if watch_id is None:
            return None

        return s.query(DbDnsResolve).select_from(DbWatchTarget) \
            .join(DbDnsResolve, DbDnsResolve.id == DbWatchTarget.last_dns_scan_id) \
            .filter(DbWatchTarget.id == watch_id) \
            .first()

    def load_last_subs_result(self, s, watch_id=None):
        """
        Loads the latest subs scan results - aggregated form
        :param s:
        :param watch_id:
        :return:
        :rtype DbSubdomainResultCache
        """
        if watch_id is None:
            return None
        q = s.query(DbSubdomainResultCache).filter(DbSubdomainResultCache.watch_id == watch_id)
        return q.order_by(DbSubdomainResultCache.last_scan_at.desc()).limit(1).first()

    def load_last_ip_scan_result(self, s, record_id=None):
        """
        Loads the latest IP scanning result
        :param s:
        :param record_id:
        :return:
        :rtype DbIpScanResult
        """
        if record_id is None:
            return None
        q = s.query(DbIpScanResult).filter(DbIpScanResult.ip_scan_record_id == record_id)
        return q.order_by(DbIpScanResult.last_scan_at.desc()).limit(1).first()

    def load_last_ip_scan_result_cached(self, s, record_id=None):
        """
        Loads the latest IP scanning result - cached variant to avoid inconsistencies
        when cache is not updated but the last result is returned fresh (frontend use caches - gets old data)

        :param s:
        :param record_id:
        :return:
        :rtype DbIpScanResult
        """
        if record_id is None:
            return None

        q = s.query(DbIpScanResult) \
            .select_from(DbLastScanCache) \
            .join(DbIpScanResult, salch.and_(
                DbLastScanCache == DbLastScanCacheType.LOCAL_SCAN,
                DbLastScanCache.scan_type == DbScanType.IP_SCAN,
                DbLastScanCache.obj_id == record_id,
                DbLastScanCache.scan_id == DbIpScanResult.id
            ))

        return q.first()

    #
    # Helpers
    #

    def interruptible_sleep(self, sleep_time):
        """
        Sleeps the current thread for given amount of seconds, stop event terminates the sleep - to exit the thread.
        :param sleep_time:
        :return:
        """
        if sleep_time is None:
            return

        sleep_time = float(sleep_time)

        if sleep_time == 0:
            return

        sleep_start = time.time()
        while self.is_running():
            time.sleep(0.1)
            if time.time() - sleep_start >= sleep_time:
                return

    def diff_time(self, delta=None, days=None, seconds=None, hours=None, rnd=True):
        """
        Returns now - diff time
        :param delta:
        :param days:
        :param seconds:
        :param hours:
        :param rnd:
        :return:
        """
        now = datetime.now()
        ndelta = delta if delta else timedelta(days=days, hours=hours, seconds=seconds)
        if rnd:
            fact = rnd if isinstance(rnd, float) else self.randomize_diff_time_fact
            ndelta += timedelta(seconds=(ndelta.total_seconds() * random.uniform(-1*fact, fact)))
        return now - ndelta

    def urlize(self, obj):
        """
        Extracts URL object
        :param obj:
        :return:
        """
        if isinstance(obj, PeriodicJob):
            return obj.url()
        elif isinstance(obj, PeriodicReconJob):
            return TargetUrl(scheme=None, host=obj.target.scan_host, port=None)
        elif isinstance(obj, PeriodicIpScanJob):
            return TargetUrl(scheme='https', host=obj.target.ip_beg)
        elif isinstance(obj, DbWatchTarget):
            return TargetUrl(scheme=obj.scan_scheme, host=obj.scan_host, port=obj.scan_port)
        elif isinstance(obj, ScanJob):
            return TargetUrl(scheme=obj.scan_scheme, host=obj.scan_host, port=obj.scan_port)
        else:
            return TlsDomainTools.urlize(obj)

    def process_handshake_certs(self, s, resp, scan_db, do_job_subres=True):
        """
        Processes certificates from the handshake
        :param s: session
        :param resp: tls scan response
        :type resp: TlsHandshakeResult
        :param scan_db: tls db model
        :type scan_db: DbHandshakeScanJob
        :param do_job_subres:
        :return:
        """
        if util.is_empty(resp.certificates):
            return

        res = self.cert_manager.process_full_chain(s, resp.certificates, is_der=True, source='handshake')
        all_certs = res[0]
        cert_existing = res[1]
        leaf_cert_id = res[2]
        num_new_results = res[3]
        all_cert_ids = [x.id for x in all_certs]

        # store non-existing certificates from the TLS scan to the database
        for cert_db in all_certs:
            fprint = cert_db.fprint_sha1

            try:
                # crt.sh scan info
                sub_res_db = DbHandshakeScanJobResult()
                sub_res_db.scan_id = scan_db.id
                sub_res_db.job_id = scan_db.job_id
                sub_res_db.was_new = fprint not in cert_existing
                sub_res_db.crt_id = cert_db.id
                sub_res_db.crt_sh_id = cert_db.crt_sh_id
                sub_res_db.is_ca = cert_db.is_ca
                sub_res_db.trans_cert = cert_db

                if do_job_subres:
                    s.add(sub_res_db)

                if not cert_db.is_ca:
                    leaf_cert_id = cert_db.id

                scan_db.trans_sub_res.append(sub_res_db)
                scan_db.trans_certs[cert_db.fprint_sha1] = cert_db

            except Exception as e:
                logger.error('Exception when processing a handshake certificate %s' % (e, ))
                self.trace_logger.log(e)

        # Scan updates with stored certs
        scan_db.cert_id_leaf = leaf_cert_id
        scan_db.new_results = num_new_results
        scan_db.certs_ids = json.dumps(sorted(all_cert_ids))

    def tls_cert_validity_test(self, resp, scan_db):
        """
        Performs TLS certificate validity test
        :param resp:
        :type resp: TlsHandshakeResult
        :param scan_db:
        :return:
        """
        # path validation test + hostname test
        try:
            validation_res = self.crt_validator.validate(resp.certificates, is_der=True)  # type: ValidationResult

            scan_db.trans_validation_res = validation_res
            scan_db.valid_path = validation_res.valid
            scan_db.err_many_leafs = len(validation_res.leaf_certs) > 1
            self.add_validation_leaf_error_to_result(validation_res, scan_db)

            # TODO: error from the validation (timeout, CA, ...)
            scan_db.err_validity = None if validation_res.valid else 'ERR'

            all_valid_alts = TlsDomainTools.get_alt_names(validation_res.valid_leaf_certs)
            matched_domains = TlsDomainTools.match_domain(resp.domain, all_valid_alts)
            scan_db.valid_hostname = len(matched_domains) > 0

        except Exception as e:
            logger.debug('Path validation failed: %s' % e)

    def connect_analysis(self, s, sys_params, resp, scan_db, domain, port=None, scheme=None, hostname=None, job_data=None):
        """
        Connects to the host, performs simple connection analysis - HTTP connect, HTTPS connect, follow redirects.
        :param s: 
        :param sys_params:
        :param resp:
        :param scan_db:
        :param domain: 
        :param port: 
        :param scheme: 
        :param hostname: 
        :param job_data:
        :return:
        """
        # scheme & port setting, params + auto-detection defaults
        scheme, port = TlsDomainTools.scheme_port_detect(scheme, port)
        hostname = util.defval(hostname, domain)

        if scheme not in ['http', 'https']:
            logger.debug('Unsupported connect scheme / port: %s / %s' % (scheme, port))
            return

        # CDN
        if job_data is not None:
            scan_db.cdn_cname = self.cname_cdn_classif.classify_cname(util.defvalkey(job_data, 'cname'))

        # Raw hostname
        test_domain = TlsDomainTools.parse_hostname(hostname)

        # Try raw connect to the tls if the previous failure does not indicate service is not running
        if resp.handshake_failure not in [TlsHandshakeErrors.CONN_ERR, TlsHandshakeErrors.READ_TO]:
            c_url = '%s://%s:%s' % (scheme, test_domain, port)

            # Direct request attempt on the url - analyze Request behaviour, headers.
            r, error = self.tls_scanner.req_connect(c_url, timeout=sys_params['timeout'], allow_redirects=False)
            scan_db.req_https_result = self.tls_scanner.err2status(error)

            # Another request for HSTS & pinning detection without cert verify.
            if error == RequestErrorCode.SSL:
                r, error = self.tls_scanner.req_connect(c_url, timeout=sys_params['timeout'], allow_redirects=False,
                                                        verify=False)
            self.http_headers_analysis(s, scan_db, r)

            r, error = self.tls_scanner.req_connect(c_url, timeout=sys_params['timeout'])
            scan_db.follow_https_result = self.tls_scanner.err2status(error)

            # Load follow URL if there was a SSL error.
            if error == RequestErrorCode.SSL:
                r, error = self.tls_scanner.req_connect(c_url, timeout=sys_params['timeout'], verify=False)

            scan_db.follow_https_url = r.url if error is None else None

        # simple HTTP check - default connection point when there is no scheme
        if port == 443:
            c_url = 'http://%s' % test_domain

            r, error = self.tls_scanner.req_connect(c_url, timeout=sys_params['timeout'])
            scan_db.follow_http_result = self.tls_scanner.err2status(error)

            # Another request without cert verify, follow url is interesting
            if error == RequestErrorCode.SSL:
                r, error = self.tls_scanner.req_connect(c_url, timeout=sys_params['timeout'], verify=False)

            scan_db.follow_http_url = r.url if error is None else None

        s.flush()

    def http_headers_analysis(self, s, scan_db, r):
        """
        HSTS / cert pinning / CDN
        :param s:
        :param scan_db:
        :param r:
        :return:
        """
        if r is None:
            return

        hsts = TlsDomainTools.detect_hsts(r)
        pinn = TlsDomainTools.detect_pinning(r)

        scan_db.hsts_present = hsts.enabled
        if hsts.enabled:
            scan_db.hsts_max_age = hsts.max_age
            scan_db.hsts_include_subdomains = hsts.include_subdomains
            scan_db.hsts_preload = hsts.preload

        scan_db.pinning_present = pinn.enabled
        if pinn.enabled:
            scan_db.pinning_report_only = pinn.report_only
            scan_db.pinning_pins = json.dumps(pinn.pins)

        # CDN detection
        scan_db.cdn_headers = TlsDomainTools.detect_cdn(r)

    def reverse_ip_analysis(self, s, sys_params, resp, scan_db, domain, job_data=None):
        """
        Reverse IP lookup + CDN detection
        :param s:
        :param sys_params:
        :param resp:
        :param scan_db:
        :param domain:
        :param job_data:
        :return:
        """
        ip = scan_db.ip_scanned
        if ip is None or ip == '-':
            return None

        try:
            addr = socket.gethostbyaddr(ip)
            scan_db.ip_scanned_reverse = util.take_last(addr[0], 254)
            scan_db.cdn_reverse = self.cname_cdn_classif.classify_cname(scan_db.ip_scanned_reverse)

        except socket.gaierror as gai:
            logger.debug('GAI error: %s: %s' % (ip, gai))

        except socket.herror as herr:
            logger.debug('Unknown host error: %s: %s' % (ip, herr))

        except Exception as e:
            logger.debug('Exception in IP reverse lookup: %s : %s' % (domain, e))
            self.trace_logger.log(e)

    def get_job_type(self, job_data):
        """
        Returns if job is UI or Background
        :param job_data:
        :return:
        """
        if job_data is None or 'sysparams' not in job_data:
            return JobType.UI
        params = job_data['sysparams']
        if 'mode' not in params:
            return JobType.UI
        return params['mode']

    def fetch_new_certs(self, s, job_data, crt_sh_id, index_result, crtsh_query_db, store_res=True):
        """
        Fetches the new cert from crt.sh, parses, inserts to the db
        :param s: 
        :param job_data: 
        :param crt_sh_id: 
        :param index_result: 
        :param crtsh_query_db: crt.sh scan object
        :param store_res: true if to store crt sh result
        :return: cert_db
        :rtype: Tuple[Certificate, DbCrtShQueryResult]
        """
        try:
            response = self.crt_sh_proc.download_crt(crt_sh_id)
            if not response.success:
                logger.debug('Download of %s not successful' % crt_sh_id)
                return None, None

            cert_db = Certificate()
            cert_db.crt_sh_id = crt_sh_id
            cert_db.crt_sh_ca_id = index_result.ca_id
            cert_db.created_at = salch.func.now()
            cert_db.pem = util.strip_pem(response.result)
            cert_db.source = 'crt.sh'
            alt_names = []

            try:
                cert = self.cert_manager.parse_certificate(cert_db, pem=str(cert_db.pem))
                alt_names = cert_db.alt_names_arr

            except Exception as e:
                cert_db.fprint_sha1 = util.try_sha1_pem(str(cert_db.pem))
                logger.error('Unable to parse certificate %s: %s' % (crt_sh_id, e))
                self.trace_logger.log(e)

            new_cert = cert_db
            cert_db, is_new = self.cert_manager.add_cert_or_fetch(s, cert_db, fetch_first=True, add_alts=True)
            if not is_new:   # cert exists, fill in missing fields if empty
                mm = Certificate
                changes = DbHelper.update_model_null_values(cert_db, new_cert, [
                    mm.crt_sh_id, mm.crt_sh_ca_id, mm.parent_id,
                    mm.key_type, mm.key_bit_size, mm.sig_alg])
                if changes > 0:
                    s.commit()

            # crt.sh scan info
            crtsh_res_db = DbCrtShQueryResult()
            crtsh_res_db.query_id = crtsh_query_db.id
            crtsh_res_db.job_id = crtsh_query_db.job_id
            crtsh_res_db.was_new = 1
            crtsh_res_db.crt_id = cert_db.id
            crtsh_res_db.crt_sh_id = crt_sh_id
            crtsh_res_db.cert_db = cert_db
            if store_res:
                s.add(crtsh_res_db)

            return cert_db, crtsh_res_db

        except Exception as e:
            logger.error('Exception when downloading a certificate %s: %s' % (crt_sh_id, e))
            self.trace_logger.log(e)
        return None, None

    def analyze_cert(self, s, job_data, cert):
        """
        Parses cert result, analyzes - adds to the db
        :param s: 
        :param job_data: 
        :param cert: 
        :return: 
        """
        return None

    def update_scan_job_state(self, job_data, state, s=None):
        """
        Updates job state in DB + sends event via redis
        :param job_data: 
        :param state: 
        :param s:
        :type s: SaQuery
        :return:
        """
        s_was_none = s is None
        try:
            if s is None:
                s = self.db.get_session()

            if isinstance(job_data, ScanJob):
                job_data.state = state
                job_data.updated_at = datetime.now()
                job_data = s.merge(job_data)
                s.flush()

            else:
                stmt = salch.update(ScanJob).where(ScanJob.uuid == job_data['uuid'])\
                    .values(state=state, updated_at=salch.func.now())
                s.execute(stmt)
                s.commit()

            # stmt = salch.update(ScanJob).where(ScanJob.uuid == job_data['uuid']).values(state=state)
            # s.execute(stmt)

        except Exception as e:
            logger.error('Scan job state update failed: %s' % e)
            self.trace_logger.log(e)

        finally:
            if s_was_none:
                util.silent_close(s)

        evt_data = {}
        if isinstance(job_data, ScanJob):
            evt_data = {'job': job_data.uuid, 'state': state}
        else:
            evt_data = {'job': job_data['uuid'], 'state': state}

        evt = rh.scan_job_progress(evt_data)
        self.redis_queue.event(evt)

    def try_whois(self, top_domain, attempts=3):
        """
        Whois call on the topdomain, with retry attempts
        :param top_domain:
        :param attempts:
        :return:
        """
        for attempt in range(attempts):
            try:
                res = ph4whois.whois(top_domain)
                return res

            except ph4whois.parser.PywhoisError as pe:
                return None  # not found

            except ph4whois.parser.PywhoisTldError as pe:
                return None  # unknown TLD

            except ph4whois.parser.PywhoisNoWhoisError as pe:
                return None  # no whois server found

            except ph4whois.parser.PywhoisSlowDownError as pe:
                logger.debug('Slow down whois warning')
                time.sleep(1.5)
                if attempt + 1 >= attempts:
                    raise

            except Exception as e:
                if attempt + 1 >= attempts:
                    raise

    def get_crtsh_query_type(self, query, default_type=None):
        """
        Determines crtsh query type CrtshInputType
        :param query:
        :param default_type:
        :return:
        """
        if default_type is None:
            default_type = CrtshInputType.EXACT

        query_type = None
        if isinstance(query, DbCrtShQueryInput):
            query_type = query.itype

        elif isinstance(query, tuple):
            query_type = query[1]

        return query_type if query_type is not None else default_type

    def get_crtsh_text_query(self, query, query_type=None):
        """
        Generates CRTSH input query to use from the input object
        :param query:
        :param query_type:
        :return:
        """
        query_input = None

        if isinstance(query, DbCrtShQueryInput):
            query_input = query.iquery
            if query_type is None:
                query_type = query.itype

        elif isinstance(query, tuple):
            query_input, query_type = query[0], query[1]

        else:
            query_input = query

        if query_type is None or query_type == CrtshInputType.EXACT:
            return str(query_input)
        elif query_type == CrtshInputType.STAR_WILDCARD:
            return '*.%s' % query_input
        elif query_type == CrtshInputType.LIKE_WILDCARD:
            return '%%.%s' % query_input
        elif query_type == CrtshInputType.RAW:
            return str(query_input)
        else:
            raise ValueError('Unknown CRTSH query type %s, input %s' % (query_type, query_input))

    def fix_sub_watch_target_domain(self, s, model):
        """
        If top domain id is not filled in, this fixes it
        :param s:
        :param model:
        :type model: DbSubdomainWatchTarget
        :return:
        """
        if model is None:
            return

        if model.top_domain_id is not None:
            return

        # top domain
        top_domain_obj, is_new = self.db_manager.try_load_top_domain(s, TlsDomainTools.parse_fqdn(model.scan_host))
        if top_domain_obj is not None:
            model.top_domain_id = top_domain_obj.id
        s.merge(model)
        s.commit()

    def is_blacklisted(self, domain):
        """
        Returns true if domain is blacklisted by some blacklist rule
        :param domain:
        :return:
        """
        blcopy = []
        with self.sub_blacklist_lock:
            blcopy = list(self.sub_blacklist)

        for rule in blcopy:
            if rule.rule_type == BlacklistRuleType.SUFFIX:
                if domain.endswith(rule.rule):
                    return rule
            elif rule.rule_type == BlacklistRuleType.MATCH:
                if domain == rule.rule:
                    return rule

        return None

    def get_validation_leaf_error(self, valres):
        """
        Tries to get validation error for leaf certificates from the result
        :param valres:
        :type valres: ValidationResult
        :return:
        """
        if valres is None or valres.leaf_validation is None:
            return None
        errs = valres.leaf_validation.validation_errors
        for idx in errs:
            if errs[idx] is not None:
                return errs[idx]

        return None

    def add_validation_leaf_error_to_result(self, valres, scan_db):
        """
        Adds leaf validation error produced by OSSL certificate validator to the scan results
        :param valres:
        :type valres: ValidationResult
        :param scan_db:
        :type scan_db: DbHandshakeScanJob
        :return:
        """
        err = self.get_validation_leaf_error(valres)
        if err is None or not isinstance(err, ValidationOsslException):
            return

        scan_db.err_valid_ossl_code = err.error_code
        scan_db.err_valid_ossl_depth = err.error_depth

    def subs_sync_records(self, s, job, db_sub):
        """
        Adds / updates DbSubdomainWatchResultEntry
        :param s:
        :param job:
        :type job: PeriodicReconJob
        :param db_sub:
        :type db_sub: DbSubdomainResultCache
        :return:
        """

        # Chunking, paginate on 50 per page
        chunks = util.chunk(db_sub.trans_result, 50)
        for chunk in chunks:
            # Load all subdomains
            subs = self.db_manager.load_subdomains(s, db_sub.watch_id, chunk)
            existing_domains = set(subs.keys())
            new_domains = set(chunk) - existing_domains

            # Update existing domains
            for cur_name in subs:
                cur_rec = subs[cur_name]  # type: DbSubdomainWatchResultEntry
                cur_rec.last_scan_at = db_sub.last_scan_at
                cur_rec.last_scan_id = db_sub.id
                cur_rec.updated_at = salch.func.now()
                cur_rec.num_scans = cur_rec.num_scans + 1
            s.commit()

            # Add new records
            for new_domain in new_domains:
                nw = DbSubdomainWatchResultEntry()
                nw.watch_id = db_sub.watch_id
                nw.is_wildcard = TlsDomainTools.has_wildcard(new_domain)
                nw.is_internal = 0
                nw.is_long = len(new_domain) > 191
                nw.name = new_domain  # TODO: hash if too long
                nw.name_full = new_domain if nw.is_long else None
                nw.created_at = salch.func.now()
                nw.updated_at = salch.func.now()
                nw.last_scan_at = db_sub.last_scan_at
                nw.first_scan_id = db_sub.id
                nw.last_scan_id = db_sub.id
                s.add(nw)
            s.commit()

    def auto_fill_assoc(self, s, assoc):
        """
        Auto fill sub domains from the association now - using current db content
        :param s:
        :param assoc:
        :type assoc: DbSubdomainWatchAssoc
        :return:
        """
        if not assoc.auto_fill_watches:
            return

        sub_res = self.load_last_subs_result(s, watch_id=assoc.watch_id)  # type: DbSubdomainResultCache
        if sub_res is None or util.is_empty(sub_res.trans_result):
            return

        self.auto_fill_new_watches_for_assoc(s, assoc, sub_res.trans_result)

    def auto_fill_new_watches(self, s, job, db_sub):
        """
        Auto-generates new watches from newly generated domains
        :param s:
        :param job:
        :type job: PeriodicReconJob
        :param db_sub:
        :type db_sub: DbSubdomainResultCache
        :return:
        """

        # load all users having auto load enabled for this one
        assocs = s.query(DbSubdomainWatchAssoc)\
            .filter(DbSubdomainWatchAssoc.watch_id == job.target.id)\
            .filter(DbSubdomainWatchAssoc.auto_fill_watches == 1)\
            .all()

        default_new_watches = {}  # type: dict[str -> DbWatchTarget]
        for assoc in assocs:  # type: DbSubdomainWatchAssoc
            self.auto_fill_new_watches_for_assoc(s, assoc, db_sub.trans_result, default_new_watches)

    def auto_fill_new_watches_for_assoc(self, s, assoc, domain_names, default_new_watches=None):
        """
        Auto-generates new watches from newly generated domains, for one association
        :param s:
        :param assoc:
        :type assoc: DbSubdomainWatchAssoc
        :param domain_names:
        :param default_new_watches: cache of loaded watch targets, used when iterating over associations
        :type default_new_watches: dict[str -> DbWatchTarget]
        :return:
        """
        if default_new_watches is None:
            default_new_watches = {}

        # number of already active hosts
        # TODO: either use PHP rest API for this or somehow get common constant config
        num_hosts = self.db_manager.load_num_active_hosts(s, owner_id=assoc.owner_id)
        max_hosts = self.config.keychest_max_servers
        if num_hosts >= max_hosts:
            return

        # Generic insertion method
        return self.auto_fill_new_watches_body(s=s,
                                               owner_id=assoc.owner_id,
                                               domain_names=domain_names,
                                               default_new_watches=default_new_watches,
                                               num_hosts=num_hosts,
                                               max_hosts=max_hosts)

    def auto_fill_new_watches_body(self, s, owner_id, domain_names, default_new_watches=None,
                                   num_hosts=0, max_hosts=None):
        """
        Helper for adding all domains to the owner_id
        :param s:
        :param owner_id:
        :param domain_names:
        :param default_new_watches:
        :param num_hosts:
        :param max_hosts:
        :return:
        """
        # select all hosts anyhow associated with the host, also deleted.
        # Wont add already present hosts (deleted/disabled doesnt matter)
        res = s.query(DbWatchAssoc, DbWatchTarget) \
            .join(DbWatchTarget, DbWatchAssoc.watch_id == DbWatchTarget.id) \
            .filter(DbWatchAssoc.owner_id == owner_id) \
            .all()  # type: list[tuple[DbWatchAssoc, DbWatchTarget]]

        # remove duplicates, extract existing association
        domain_names = util.stable_uniq(domain_names)
        existing_host_names = set([x[1].scan_host for x in res])
        default_new_watches = dict() if default_new_watches is None else default_new_watches

        for new_host in domain_names:
            if max_hosts is not None and num_hosts >= max_hosts:
                logger.debug('User %s reached max hosts %s, not adding more' % (owner_id, max_hosts))
                break

            if new_host in existing_host_names:
                continue

            new_host = TlsDomainTools.parse_fqdn(new_host)
            if new_host in existing_host_names:
                continue

            if not TlsDomainTools.can_connect(new_host):
                logger.debug('Not going to add host %s to user %s, invalid host name' % (new_host, owner_id))
                continue

            wtarget = None
            if new_host in default_new_watches:
                wtarget = default_new_watches[new_host]
            else:
                wtarget, wis_new = self.db_manager.load_default_watch_target(s, new_host)
                default_new_watches[new_host] = wtarget
                s.commit()  # if add fails the rollback removes the watch
                if wis_new:
                    logger.debug('New watch_target added for association: %s id %s' % (new_host, wtarget.id))

            # new association
            nassoc = DbWatchAssoc()
            nassoc.owner_id = owner_id
            nassoc.watch_id = wtarget.id
            nassoc.updated_at = salch.func.now()
            nassoc.created_at = salch.func.now()
            nassoc.auto_scan_added_at = salch.func.now()

            # race condition with another process may cause this to fail on unique constraint.
            try:
                s.add(nassoc)
                s.commit()

                num_hosts += 1
                existing_host_names.add(new_host)
                logger.debug('New host %s ID %s associated to the owner %s assoc ID %s, hosts: %s'
                             % (new_host, wtarget.id, owner_id, nassoc.id, num_hosts))

            except Exception as e:
                logger.debug('Exception when adding auto watch: %s' % e)
                self.trace_logger.log(e, custom_msg='Auto add watch')
                util.silent_rollback(s)

    def auto_fill_ip_watches(self, s, job, db_sub):
        """
        Auto creates IP scan based watchers
        :param s:
        :param job:
        :type job: PeriodicIpScanJob
        :param db_sub:
        :type db_sub: DbIpScanResult
        :return:
        """
        # fetch watcher target defined for this IP scan record, rewrite its DNS result
        record = s.query(DbWatchTarget).filter(DbWatchTarget.ip_scan_id == job.target.id).first()
        record_exists = record is not None

        # create watch if does not exist
        if not record_exists:
            target = s.merge(job.target)
            record = DbWatchTarget()
            record.ip_scan_id = target.id
            record.created_at = salch.func.now()
            record.updated_at = salch.func.now()
            record.service_id = target.service_id
            record.top_domain_id = target.service.top_domain_id
            record.ip_scan_id = target.id

            record.manual_dns = True
            record.scan_host = target.service_name
            record.scan_port = 443  # configurable port
            record.scan_scheme = 'https'  # configurable scheme
            s.add(record)
            s.flush()

        # create DNS record if does not exist
        last_dns = record.last_dns_scan
        last_dns_exists = last_dns is not None

        new_dns = self.ip_scan_to_dns(db_sub)
        new_dns.watch_id = record.id
        new_dns_differ = not last_dns_exists and not self.diff_scan_dns(last_dns, new_dns)

        dns_updated = False

        if not last_dns_exists or new_dns_differ:
            record.last_dns_scan = new_dns
            s.add(new_dns)
            s.flush()
            dns_updated = True

        s.commit()
        self.auto_fill_ip_watches_assoc(s, job, db_sub, record)

    def auto_fill_ip_watches_assoc(self, s, job, db_sub, record):
        """
        Creates a new associations to the given watch target generated by IP scanner.
        :param s:
        :param job:
        :type job: PeriodicIpScanJob
        :param db_sub:
        :type db_sub: DbIpScanResult
        :param record:
        :type record: DbWatchTarget
        :return:
        """

        # load all users having auto load enabled for this one
        assocs = s.query(DbIpScanRecordUser) \
            .filter(DbIpScanRecordUser.ip_scan_record_id == job.target.id) \
            .filter(DbIpScanRecordUser.auto_fill_watches == 1) \
            .all()
        
        for assoc in assocs:  # type: DbIpScanRecordUser
            self.auto_fill_ip_watches_for_assoc(s, job, db_sub, record, assoc)

    def auto_fill_ip_watches_for_assoc(self, s, job, db_sub, record, assoc):
        """
        Creates a new association of the IP scan generated watch target to the user
        :param s:
        :param job:
        :type job: PeriodicIpScanJob
        :param db_sub:
        :type db_sub: DbIpScanResult
        :param record:
        :type record: DbWatchTarget
        :param assoc:
        :type assoc: DbIpScanRecordUser
        :return:
        """
        # number of already active hosts
        # TODO: either use PHP rest API for this or somehow get common constant config
        num_hosts = self.db_manager.load_num_active_hosts(s, owner_id=assoc.owner_id)
        max_hosts = self.config.keychest_max_servers
        if num_hosts >= max_hosts:
            return

        # If there is some record of the association, do not add a new one.
        # If association has been deleted / disabled it is left in the current state.
        res = s.query(DbWatchAssoc) \
            .filter(DbWatchAssoc.owner_id == assoc.owner_id) \
            .filter(DbWatchAssoc.watch_id == record.id) \
            .first()  # type: DbWatchAssoc
        if res is not None:
            return

        # add new user <-> watch target new association
        nassoc = DbWatchAssoc()
        nassoc.owner_id = assoc.owner_id
        nassoc.watch_id = record.id
        nassoc.updated_at = salch.func.now()
        nassoc.created_at = salch.func.now()
        nassoc.auto_scan_added_at = salch.func.now()

        # race condition with another process may cause this to fail on unique constraint.
        try:
            s.add(nassoc)
            s.commit()

        except Exception as e:
            logger.error('Exception when adding auto ip watch: %s' % e)
            self.trace_logger.log(e, custom_msg='Auto add ip watch')
            s.rollback()

    #
    # DB tools
    #

    def try_get_top_domain(self, domain):
        """
        try-catched top domain load
        :param domain:
        :return:
        """
        try:
            return TlsDomainTools.get_top_domain(domain)
        except:
            pass

    def ip_scan_to_dns(self, scan):
        """
        Transforms IP scan result to the synthetic DNS result
        :param scan:
        :type scan: DbIpScanResult
        :return:
        """
        ipset = [(IpType.NET_IPv4, ip) for ip in scan.trans_ips_found]

        dns = DbDnsResolve()
        dns.created_at = salch.func.now()
        dns.updated_at = salch.func.now()
        dns.last_scan_at = salch.func.now()
        dns.num_scans = 1
        dns.status = 1
        dns.dns_status = 1
        dns.is_synthetic = True

        dns.dns_res = ipset  # [(2, ipv4), (10, ipv6)]
        dns.dns = json.dumps(ipset)
        dns.num_res = len(ipset)
        dns.num_ipv4 = len([x for x in dns.dns_res if x[0] == IpType.NET_IPv4])
        dns.num_ipv6 = 0
        dns.entries = []

        for idx, ip in enumerate(scan.trans_ips_found):
            en = DbDnsEntry()
            en.scan = dns
            en.is_ipv6 = False
            en.is_internal = TlsDomainTools.is_ip_private(ip)
            en.ip = ip
            en.res_order = idx

            dns.entries.append(en)
        return dns

    #
    # Workers - Redis interactive jobs
    #

    def worker_main(self, idx):
        """
        Worker main entry method - worker thread executes this.
        Processes job_queue jobs, mainly redis enqueued.

        :param idx: 
        :return: 
        """
        self.local_data.idx = idx
        logger.info('Worker %02d started' % idx)

        while self.is_running():
            job = None
            try:
                job = self.job_queue.get(True, timeout=1.0)
            except QEmpty:
                time.sleep(0.1)
                continue

            try:
                # Process job in try-catch so it does not break worker
                logger.info('[%02d] Processing job' % (idx, ))
                jtype, jobj = job
                if jtype == 'redis':
                    self.process_redis_job(jobj)
                else:
                    pass

            except Exception as e:
                logger.error('Exception in processing job %s: %s' % (e, job))
                self.trace_logger.log(e)

            finally:
                self.job_queue.task_done()
        logger.info('Worker %02d terminated' % idx)

    def scan_load_redis_job(self):
        """
        Loads redis job from the queue. Blocking behavior for optimized performance
        :return: 
        """
        job = self.redis_queue.pop(blocking=True, timeout=1)
        if job is None:
            raise QEmpty()

        return job

    def scan_redis_jobs(self):
        """
        Blocking method scanning redis jobs.
        Should be run in dedicated thread or in the main thread as it blocks the execution. 
        :return: 
        """
        cur_size = self.redis_queue.size()
        logger.info('Redis total queue size: %s' % cur_size)

        while self.is_running():
            job = None
            try:
                job = self.scan_load_redis_job()

            except QEmpty:
                time.sleep(0.01)
                continue

            try:
                self.job_queue.put(('redis', job))

            except Exception as e:
                logger.error('Exception in processing job %s' % (e, ))
                self.trace_logger.log(e)

            finally:
                pass
        logger.info('Queue scanner terminated')

    #
    # Eventing
    #

    def on_new_scan(self, s, old_scan, new_scan, job=None):
        """
        Event called on a new scan result.
        :param s:
        :param old_scan:
        :param new_scan:
        :param job:
        :return:
        """
        if not self.agent_mode:
            return  # nothing to do in server mode now. Later - recomputation, caching, UI eventing, ...

        self.mod_agent.agent_on_new_scan(s, old_scan=old_scan, new_scan=new_scan, job=job)

    #
    # DB cleanup
    #

    def cleanup_main(self):
        """
        DB trimming & general cleanup thread
        :return:
        """
        logger.info('Cleanup thread started %s %s %s' % (os.getpid(), os.getppid(), threading.current_thread()))
        try:
            while not self.stop_event.is_set():
                try:
                    time.sleep(0.2)
                    cur_time = time.time()
                    if self.cleanup_last_check + self.cleanup_check_time > cur_time:
                        continue

                    self.reload_blacklist()

                    # TODO: clean old RRD records
                    self.cleanup_last_check = cur_time

                except Exception as e:
                    logger.error('Exception in DB cleanup: %s' % e)
                    self.trace_logger.log(e)

        except Exception as e:
            logger.error('Exception: %s' % e)
            self.trace_logger.log(e)

        logger.info('Cleanup loop terminated')

    def reload_blacklist(self):
        """
        Reloads sub-blacklist
        :return:
        """
        s = None
        try:
            s = self.db.get_session()
            blacklist_db = s.query(DbSubdomainScanBlacklist).all()

            with self.sub_blacklist_lock:
                self.sub_blacklist = blacklist_db

        finally:
            util.silent_close(s)

    def state_main(self):
        """
        State main thread
        :return:
        """
        logger.info('State thread started %s %s %s' % (os.getpid(), os.getppid(), threading.current_thread()))
        try:
            while not self.stop_event.is_set():
                try:
                    time.sleep(0.5)
                    cur_time = time.time()
                    if self.state_last_check + 10 > cur_time:
                        continue

                    self.state_ram_check()
                    self.state_last_check = cur_time

                except Exception as e:
                    logger.error('Exception in state thread: %s' % e)
                    self.trace_logger.log(e)

        except Exception as e:
            logger.error('Exception: %s' % e)
            self.trace_logger.log(e)

        logger.info('State loop terminated')

    def state_ram_check(self):
        """
        Checks memory terminating conditions
        :return:
        """

        if self.args.max_mem is None:
            return

        cur_ram = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.
        if cur_ram <= self.args.max_mem:
            return

        logger.warning('Maximum memory threshold reached: %s MB, threshold = %s MB' % (cur_ram, self.args.max_mem))
        self.trigger_stop()

    #
    # Migration
    #

    def migrate_main(self):
        """
        Live data migration to minimize downtime
        :return:
        """
        logger.info('Migration thread started %s %s %s' % (os.getpid(), os.getppid(), threading.current_thread()))
        s = None
        try:
            def should_terminate():
                """
                Migration should terminate lambda
                :return:
                """
                return self.stop_event.is_set() or self.terminate

            s = self.db.get_session()
            mig_mgr = DbMigrationManager(s=s, should_terminate=should_terminate)
            mig_mgr.migrate()

        except Exception as e:
            logger.error('Exception in DB migration: %s' % e)
            self.trace_logger.log(e)

        finally:
            util.silent_expunge_all(s)
            util.silent_close(s)

        logger.info('Migration thread terminated')

    #
    # Server
    #

    def start_daemon(self):
        """
        Starts daemon mode
        :return:
        """
        self.daemon = AppDeamon('/var/run/enigma-keychest-server.pid',
                                stderr=os.path.join(self.logdir, "stderr.log"),
                                stdout=os.path.join(self.logdir, "stdout.log"),
                                app=self)
        self.daemon.start()

    def shutdown_server(self):
        """
        Shutdown flask server
        :return:
        """

    def terminating(self):
        """
        Set state to terminating
        :return:
        """
        self.running = False
        self.stop_event.set()

        for server_mod in self.modules:
            server_mod.shutdown()

    def run_modules(self):
        """
        Run all modules
        :return:
        """
        for server_mod in self.modules:
            server_mod.run()

    def init_api(self):
        """
        Initializes rest endpoint
        :return:
        """
        if not self.config.enable_rest_api:
            logger.info('REST API disabled by configuration')
            return

        self.api = RestAPI()
        self.api.server = self
        self.api.config = self.config
        self.api.db = self.db
        self.api.debug = False  # self.args.debug # reloader does not work outside main thread.
        self.api.start()

    def work(self):
        """
        Main work method for the server - accepting incoming connections.
        :return:
        """
        logger.info('Main thread started %s %s %s' % (os.getpid(), os.getppid(), threading.current_thread()))

        # Main working loop depends on the operation mode
        if self.agent_mode:
            self.work_agent_main()
        else:
            self.work_redis_scan_main()

        self.terminating()
        logger.info('Work loop terminated')

    def work_agent_main(self):
        """
        Main agent work loop
        :return:
        """
        while self.is_running():
            time.sleep(0.5)

    def work_redis_scan_main(self):
        """
        Main thread scanning the redis queue for jobs
        :return:
        """
        try:
            # scan redis queue infinitelly
            self.scan_redis_jobs()
            logger.info('Terminating')

            # Wait on all jobs being finished
            self.job_queue.join()

            # All data processed, terminate bored workers
            self.stop_event.set()

            # Make sure it is over by joining threads
            for th in self.workers:
                th.join()

        except Exception as e:
            logger.error('Exception: %s' % e)
            self.trace_logger.log(e)

    def work_loop(self):
        """
        Process configuration, initialize connections, databases, start threads.
        :return:
        """
        # Init
        self.init_config()
        self.init_log()
        self.init_db()
        self.init_misc()
        self.init_modules()
        util.monkey_patch_asn1_time()

        self.cleanup_thread = threading.Thread(target=self.cleanup_main, args=())
        self.cleanup_thread.setDaemon(True)
        self.cleanup_thread.start()

        self.state_thread = threading.Thread(target=self.state_main, args=())
        self.state_thread.setDaemon(True)
        self.state_thread.start()

        migrate_thread = threading.Thread(target=self.migrate_main, args=())
        migrate_thread.setDaemon(True)
        migrate_thread.start()

        # Worker start
        for worker_idx in range(0, self.config.workers):
            t = threading.Thread(target=self.worker_main, args=(worker_idx, ))
            self.workers.append(t)
            t.setDaemon(True)
            t.start()

        # watcher feeder thread
        self.periodic_feeder_init()
        self.watcher_thread = threading.Thread(target=self.periodic_feeder_main, args=())
        self.watcher_thread.setDaemon(True)
        self.watcher_thread.start()

        # executes all modules kick off
        self.run_modules()

        # REST server needed only for master mode for now (may be changed in future).
        # Init agent mode if needed.
        if self.agent_mode:
            logger.info(' ==== Keychest scanner running in the Agent mode ==== ')
            self.mod_agent.init_agent()
        else:
            logger.info(' ==== Keychest scanner running in the Master mode ==== ')
            self.init_api()

        # Daemon vs. run mode.
        if self.args.daemon:
            logger.info('Starting daemon')
            self.start_daemon()

        else:
            # if not self.check_pid():
            #     return self.return_code(1)
            self.work()

    def app_main(self):
        """
        Argument parsing & startup
        :return:
        """
        # Parse our argument list
        parser = argparse.ArgumentParser(description='EnigmaBridge keychest server')

        parser.add_argument('-l', '--pid-lock', dest='pidlock', type=int, default=-1,
                            help='number of attempts for pidlock acquire')

        parser.add_argument('--debug', dest='debug', default=False, action='store_const', const=True,
                            help='enables debug mode')

        parser.add_argument('--server-debug', dest='server_debug', default=False, action='store_const', const=True,
                            help='enables server debug mode')

        parser.add_argument('--verbose', dest='verbose', action='store_const', const=True,
                            help='enables verbose mode')

        parser.add_argument('-d', '--daemon', dest='daemon', default=False, action='store_const', const=True,
                            help='Runs in daemon mode')

        parser.add_argument('--ebstall', dest='ebstall', default=False, action='store_const', const=True,
                            help='ebstall compatible mode - uses enigma configuration')

        parser.add_argument('--dump-stats', dest='dump_stats_file', default=None,
                            help='Dumping stats to a file')

        parser.add_argument('--max-mem', dest='max_mem', default=None, type=float,
                            help='Maximal memory threshold in MB when program terminates itself')

        parser.add_argument('--no-jobs', dest='no_jobs', default=False, action='store_const', const=True,
                            help='Disables watch jobs processing, for debugging')

        self.args = parser.parse_args()
        if self.args.debug:
            coloredlogs.install(level=logging.DEBUG)

        util.install_sarge_filter()
        self.work_loop()


def main():
    """
    Main server starter
    :return:
    """
    app = Server()
    app.app_main()


if __name__ == '__main__':
    main()

