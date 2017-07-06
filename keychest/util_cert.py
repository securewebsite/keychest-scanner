#!/usr/bin/env python
# -*- coding: utf-8 -*-

import cryptography.x509.oid as coid
import util
import consts
import re

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey
from cryptography.hazmat.primitives.asymmetric.dsa import DSAPublicKey
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePublicKey


def try_get_key_type(pub):
    """
    Determines pubkey type
    :param pub:
    :return:
    """
    if isinstance(pub, RSAPublicKey):
        return consts.CertKeyType.RSA
    elif isinstance(pub, DSAPublicKey):
        return consts.CertKeyType.DSA
    elif isinstance(pub, EllipticCurvePublicKey):
        return consts.CertKeyType.ECC
    else:
        return -1


def try_get_pubkey_size(pub):
    """
    Determines public key bit size
    :param pub:
    :return:
    """
    if isinstance(pub, RSAPublicKey):
        return pub.key_size
    elif isinstance(pub, DSAPublicKey):
        return pub.key_size
    elif isinstance(pub, EllipticCurvePublicKey):
        return pub.key_size
    else:
        return -1


def cloudflare_altnames(altnames):
    """
    Returns cloudflares alt names
    :param altnames:
    :return:
    """
    return [x for x in altnames if
            x is not None and (
                x.endswith('.cloudflaressl.com') or
                re.match(r'^ssl[0-9]+.cloudflare.com$', x))]
