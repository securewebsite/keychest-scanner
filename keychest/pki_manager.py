#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
PKI, certificate managers and processors
"""

from past.builtins import basestring  # pip install future
from past.builtins import cmp
from future.utils import iteritems

import json
from cryptography.x509 import Certificate as X509Certificate

from . import util, util_cert
from .consts import CertSigAlg


class PkiManager(object):
    """
    Base PKI manager for certificate related tasks
    """

    def __init__(self):
        self.db = None
        self.config = None

    def init(self, **kwargs):
        """
        Initializes the PKI manager
        :param kwargs:
        :return:
        """
        if 'db' in kwargs:
            self.db = kwargs.get('db')
        if 'config' in kwargs:
            self.config = kwargs.get('config')

