#!/usr/bin/env python
# -*- coding: utf-8 -*-

import sys
import traceback

__author__ = 'dusanklinec'


def error_message(e, message=None, cause=None):
    """
    Formats exception message + cause
    :param e:
    :param message:
    :param cause:
    :return: formatted message, includes cause if any is set
    """
    if message is None and cause is None:
        return None
    elif message is None:
        return '%s, caused by %r' % (e.__class__, cause)
    elif cause is None:
        return message
    else:
        return '%s, caused by %r' % (message, cause)


class Error(Exception):
    """Generic EB client error."""
    def __init__(self, message=None, cause=None, do_message=True):
        super(Error, self).__init__(error_message(self, message, cause))
        self.cause = cause
        self.message = message
        self.base_message = message

        self.exc_type, self.exc_value, self.exc_traceback = None, None, None
        self.traceback_formatted = None
        self.traceback = None

        self.load(cause, do_message=do_message)

    def load(self, cause=None, do_message=True):
        """
        Loads exception data from the current exception frame - should be called inside the except block
        :return:
        """
        if cause is not None:
            self.cause = cause
            if do_message:
                self.message = error_message(self, self.base_message, cause)

        self.exc_type, self.exc_value, self.exc_traceback = sys.exc_info()
        self.traceback_formatted = traceback.format_exc()
        self.traceback = traceback.extract_tb(self.exc_traceback)
        return self


class InvalidResponse(Error):
    """Invalid server response"""
    def __init__(self, message=None, cause=None):
        super(InvalidResponse, self).__init__(message=message, cause=cause)


class InvalidStatus(Error):
    """Invalid server response"""
    def __init__(self, message=None, cause=None):
        super(InvalidStatus, self).__init__(message=message, cause=cause)


class InvalidState(Error):
    """Invalid internal state"""
    def __init__(self, message=None, cause=None):
        super(InvalidState, self).__init__(message=message, cause=cause)


class RequestFailed(Error):
    """API request failed"""
    def __init__(self, message=None, cause=None):
        super(RequestFailed, self).__init__(message=message, cause=cause)


class EnvError(Error):
    """Problem with the environment running the script"""
    def __init__(self, message=None, cause=None):
        super(EnvError, self).__init__(message=message, cause=cause)


class SubprocessError(Error):
    """Error when executing a subprocess"""
    def __init__(self, message=None, cause=None):
        super(SubprocessError, self).__init__(message=message, cause=cause)


class AccessForbiddenError(Error):
    """Access to the resource was forbidden"""
    def __init__(self, message=None, cause=None):
        super(AccessForbiddenError, self).__init__(message=message, cause=cause)


class RequestError(Error):
    """Access to the resource was forbidden"""
    def __init__(self, message=None, cause=None):
        super(RequestError, self).__init__(message=message, cause=cause)


class InvalidHostname(Error):
    """Cannot connect to the host"""
    def __init__(self, message=None, cause=None):
        super(InvalidHostname, self).__init__(message=message, cause=cause)


class ServerShuttingDown(Error):
    """Server is shutting down"""
    def __init__(self, message=None, cause=None):
        super(ServerShuttingDown, self).__init__(message=message, cause=cause)


class InvalidInputData(Error):
    """Invalid test input data"""
    def __init__(self, message=None, cause=None):
        super(InvalidInputData, self).__init__(message=message, cause=cause)


class OsError(Error):
    """Invalid test input data"""
    def __init__(self, message=None, cause=None):
        super(OsError, self).__init__(message=message, cause=cause)


class AnsibleError(Error):
    """Root for Ansible errors"""
    def __init__(self, message=None, cause=None):
        super(AnsibleError, self).__init__(message=message, cause=cause)


class AnsibleRuntimeError(Error):
    """Root for Ansible runtime errors"""
    def __init__(self, message=None, cause=None):
        super(AnsibleRuntimeError, self).__init__(message=message, cause=cause)


class PkiNotSupported(AnsibleError):
    """Root for Ansible errors"""
    def __init__(self, message=None, cause=None):
        super(PkiNotSupported, self).__init__(message=message, cause=cause)


