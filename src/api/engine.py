"""
    The engine for the metrics API.  Stores definitions an backend API operations.
"""
__author__ = "ryan faulkner"
__email__ = "rfaulkner@wikimedia.org"
__date__ = "january 11 2012"
__license__ = "GPL (version 2 or later)"

from flask import escape, redirect, url_for
from src.utils.record_type import *
from src.api import COHORT_REGEX, parse_cohorts, MetricsAPIError
from dateutil.parser import parse as date_parse
from datetime import timedelta, datetime
from re import search
from collections import OrderedDict

import src.etl.data_loader as dl
import src.metrics.metrics_manager as mm

from config import logging

# Define the standard variable names in the query string - store in named tuple
RequestMeta = recordtype('RequestMeta', 'cohort_expr cohort_gen_timestamp metric time_series ' +\
                                        'aggregator date_start date_end interval t n')

def RequestMetaFactory(cohort_expr, cohort_gen_timestamp, metric, time_series, aggregator, date_start, date_end,
                       interval=None, t=None, n=None):
    return RequestMeta(cohort_expr, cohort_gen_timestamp, metric, time_series, aggregator, date_start, date_end,
        interval, t, n)

REQUEST_META_QUERY_STR = ['aggregator', 'time_series', 'date_start', 'date_end', 'interval', 't', 'n']
REQUEST_META_BASE = ['cohort_expr', 'metric']

# Datetime string format to be used throughout the API
DATETIME_STR_FORMAT = "%Y%m%d%H%M%S"

def process_request_params(request_meta):
    """
        Applies defaults and consistency to RequestMeta data

            request_meta - RequestMeta recordtype.  Stores the request data.
    """

    DEFAULT_INTERVAL = 1
    TIME_STR = '000000'

    end = datetime.now()
    start= end + timedelta(days=-DEFAULT_INTERVAL)

    # Handle any datetime fields passed - raise an exception if the formatting is incorrect
    if request_meta.date_start:
        try:
            request_meta.date_start = date_parse(request_meta.date_start).strftime(DATETIME_STR_FORMAT)[:8] + TIME_STR
        except ValueError:
            raise MetricsAPIError('1') # Pass the value of the error code in `error_codes`
    else:
        request_meta.date_start = start.strftime(DATETIME_STR_FORMAT)[:8] + TIME_STR

    if request_meta.date_end:
        try:
            request_meta.date_end = date_parse(request_meta.date_end[:8]).strftime(DATETIME_STR_FORMAT) + TIME_STR
        except ValueError:
            raise MetricsAPIError('1') # Pass the value of the error code in `error_codes`
    else:
        request_meta.date_end = end.strftime(DATETIME_STR_FORMAT)[:8] + TIME_STR

    request_meta.time_series = True if request_meta.time_series else None

    agg_key = mm.get_agg_key(request_meta.metric, request_meta.aggregator)
    request_meta.aggregator = agg_key if agg_key else None


def get_users(cohort_expr):
    """ get users from cohort """

    if search(COHORT_REGEX, cohort_expr):
        logging.info(__name__ + '::Processing cohort by expression.')
        users = [user for user in parse_cohorts(cohort_expr)]
    else:
        logging.info(__name__ + '::Processing cohort by tag name.')
        conn = dl.Connector(instance='slave')
        try:
            conn._cur_.execute('select utm_id from usertags_meta where utm_name = "%s"' % str(cohort_expr))
            res = conn._cur_.fetchone()[0]
            conn._cur_.execute('select ut_user from usertags where ut_tag = "%s"' % res)
        except IndexError:
            redirect(url_for('cohorts'))
        users = [r[0] for r in conn._cur_]
        del conn
    return users

def get_cohort_id(utm_name):
    """ Pull cohort ids from cohort handles """
    conn = dl.Connector(instance='slave')
    conn._cur_.execute('SELECT utm_id FROM usertags_meta WHERE utm_name = "%s"' % str(escape(utm_name)))

    utm_id = None
    try: utm_id = conn._cur_.fetchone()[0]
    except ValueError: pass

    # Ensure the field was retrieved
    if not utm_id:
        logging.error(__name__ + '::Missing utm_id for cohort %s.' % str(utm_name))
        utm_id = -1

    del conn
    return utm_id

def get_cohort_refresh_datetime(utm_id):
    """ Get the latest refresh datetime of a cohort.  Returns current time formatted as a
     string if the field is not found. """
    conn = dl.Connector(instance='slave')
    conn._cur_.execute('SELECT utm_touched FROM usertags_meta WHERE utm_id = %s' % str(escape(utm_id)))

    utm_touched = None
    try: utm_touched = conn._cur_.fetchone()[0]
    except ValueError: pass

    # Ensure the field was retrieved
    if not utm_touched:
        logging.error(__name__ + '::Missing utm_touched for cohort %s.' % str(utm_id))
        utm_touched = datetime.now()

    del conn
    return utm_touched.strftime(DATETIME_STR_FORMAT)

def get_data(request_meta, hash_table_ref):
    """ Extract data from the global hash given a request object """

    # Traverse the hash key structure to find data
    for key_name in REQUEST_META_BASE + REQUEST_META_QUERY_STR:
        key = getattr(request_meta,key_name)
        if not key: continue  # Only process keys that have been set
        if hasattr(hash_table_ref, 'has_key') and hash_table_ref.has_key(key):
            hash_table_ref = hash_table_ref[key]
        else:
            return None

    # Ensure that an interface that does not rely on keyed values is returned
    # all data must be in interfaces resembling lists
    if not hasattr(hash_table_ref, '__iter__'):
        return hash_table_ref
    else:
        return None

def set_data(request_meta, data, hash_table_ref):
    """ Given request meta-data and a dataset create a key path in the global hash to store the data """

    key_sig = list()

    # Build the key signature
    for key_name in REQUEST_META_BASE: # These keys must exist
        key = getattr(request_meta, key_name)
        if key:
            key_sig.append(key)
        else:
            logging.error(__name__ + '::Request must include %s. Cannot set data %s.' % (key_name, str(request_meta)))
            return

    for key_name in REQUEST_META_QUERY_STR: # These keys may optionally exist
        key = getattr(request_meta, key_name)
        if key: key_sig.append(key)

    # For each key in the key signature add a nested key to the hash
    last_item = key_sig[len(key_sig) - 1]
    for key in key_sig:
        if key != last_item:
            if not (hasattr(hash_table_ref, 'has_key') and hash_table_ref.has_key(key)):
                hash_table_ref[key] = OrderedDict()
            hash_table_ref = hash_table_ref[key]
        else:
            hash_table_ref[key] = data