"""
    This module implements the request manager functionality.

    Job Queue and Processing
    ^^^^^^^^^^^^^^^^^^^^^^^^

    As requests are issued via http to the API a process queue will store all
    active jobs. Processes will be created and assume one of the following
    states throughout their existence: ::

        * 'pending' - The request has yet to be begin being processed
        * 'running' - The request is being processed
        * 'success' - The request has finished processing and is exposed at
            the url
        * 'failure' - The result has finished processing but dailed to expose
            results

    When a process a request is received and a job is created to service that
    request it enters the 'pending' state. If the job returns without
    exception it enters the 'success' state, otherwise it enters the 'failure'
    state.  The job remains in either of these states until it is cleared
    from the process queue.

    Response Data
    ^^^^^^^^^^^^^

    As requests are made to the API the data generated and formatted as JSON.
    The definition of is as follows: ::

        {   header : header_list,
            cohort_expr : cohort_gen_timestamp : metric : timeseries :
            aggregator : start : end : [ metric_param : ]* : data
        }

    Where each component is defined: ::

        header_str := list(str), list of header values
        cohort_expr := str, cohort ID expression
        cohort_gen_timestamp := str, cohort generation timestamp (earliest of
            all cohorts in expression)
        metric := str, user metric handle
        timeseries := boolean, indicates if this is a timeseries
        aggregator := str, aggregator used
        start := str, start datetime of request
        end := str, end datetime of request
        metric_param := -, optional metric parameters
        data := list(tuple), set of data points

    Request data is mapped to a query via metric objects and hashed in the
    dictionary `api_data`.

    Request Flow Management
    ^^^^^^^^^^^^^^^^^^^^^^^

    This portion of the module defines a set of methods useful in handling
    series of metrics objects to build more complex results.  This generally
    involves creating one or more UserMetric derived objects with passed
    parameters to service a request.  The primary entry point is the
    ``process_data_request`` method. This method coordinates requests for
    three different top-level request types:

    - **Raw requests**.  Output is a set of datapoints that consist of the
      user IDs accompanied by metric results.
    - **Aggregate requests**.  Output is an aggregate of all user results based
      on the type of aggregaion as defined in the aggregator module.
    - **Time series requests**.  Outputs a time series list of data.  For this
      type of request a start and end time must be defined along with an
      interval length.  Further an aggregator must be provided which operates
      on each time interval.

    Also defined are metric types for which requests may be made with
    ``metric_dict``, and the types of aggregators that may be called on metrics
    ``aggregator_dict``, and also the meta data around how many threads may be
    used to process metrics ``USER_THREADS`` and ``REVISION_THREADS``.

"""

__author__ = {
    "ryan faulkner": "rfaulkner@wikimedia.org"
}
__date__ = "2013-03-05"
__license__ = "GPL (version 2 or later)"

from user_metrics.config import logging, settings
from user_metrics.api.engine import MAX_CONCURRENT_JOBS, \
    QUEUE_WAIT, MW_UID_REGEX
from user_metrics.api import MetricsAPIError
from user_metrics.api.engine.data import get_users
from user_metrics.api.engine.request_meta import rebuild_unpacked_request
from user_metrics.metrics.users import MediaWikiUser
from user_metrics.utils import unpack_fields

from multiprocessing import Process, Queue
from collections import namedtuple
from re import search
from os import getpid
from sys import getsizeof


# API JOB HANDLER
# ###############

# Determines maximum block size of queue item
MAX_BLOCK_SIZE = 5000

# Defines the job item type used to temporarily store job progress
job_item_type = namedtuple('JobItem', 'id process request queue')


def job_control(request_queue, response_queue):
    """
        Controls the execution of user metrics requests

        Parameters
        ~~~~~~~~~~

        request_queue : multiprocessing.Queue
           Queues incoming API requests.

    """

    # Store executed and pending jobs respectively
    job_queue = list()
    wait_queue = list()

    # Global job ID number
    job_id = 0

    # Tallies the number of concurrently running jobs
    concurrent_jobs = 0

    logging.debug('{0} :: {1}  - STARTING...'
    .format(__name__, job_control.__name__))

    while 1:

        # Request Queue Processing
        # ------------------------

        try:
            # Pull an item off of the queue
            req_item = request_queue.get(timeout=QUEUE_WAIT)

            logging.debug('{0} :: {1}' \
                          '\n\tPull item from request queue -> \n\t{2}'
            .format(__name__, job_control.__name__, str(req_item)))

        except Exception:
            req_item = None
            #logging.debug('{0} :: {1}  - Listening ...'
            #.format(__name__, job_control.__name__))


        # Process complete jobs
        # ---------------------

        for job_item in job_queue:

            # Try to pull some data off of the queue without blocking
            data = job_item.queue.get(False)

            # Look for completed jobs
            if data:

                # Pull remaining data off of the queue
                # and add it to the queue data
                stream = ''
                while data:
                    stream += data
                    if not job_item.queue.empty():
                        data = job_item.queue.get(timeout=0.1)
                    else:
                        data = None

                data = eval(stream)
                response_queue.put([unpack_fields(job_item.request), data])

                del job_queue[job_queue.index(job_item)]

                concurrent_jobs -= 1

                logging.debug('{0} :: {1}\n\tRUN -> RESPONSE {2}\n' \
                              '\tConcurrent jobs = {3}'
                .format(__name__, job_control.__name__, str(job_item),
                        concurrent_jobs))


        # Process pending jobs
        # --------------------

        for wait_req in wait_queue:
            if concurrent_jobs <= MAX_CONCURRENT_JOBS:
                # prepare job from item

                req_q = Queue()
                proc = Process(target=process_metrics, args=(req_q, wait_req))
                proc.start()

                job_item = job_item_type(job_id, proc, wait_req, req_q)
                job_queue.append(job_item)

                del wait_queue[wait_queue.index(wait_req)]

                concurrent_jobs += 1
                job_id += 1

                logging.debug('{0} :: {1}\n\tWAIT -> RUN {2}\n' \
                              '\tConcurrent jobs = {3}'\
                .format(__name__, job_control.__name__, str(wait_req),
                        concurrent_jobs))


        # Add newest job to the queue
        # ---------------------------

        if req_item and concurrent_jobs <= MAX_CONCURRENT_JOBS:

            # Build the request item
            rm = rebuild_unpacked_request(req_item)

            logging.debug('{0} :: {1}\n\tREQUEST -> WAIT {2}'
            .format(__name__, job_control.__name__, str(req_item)))
            wait_queue.append(rm)


    logging.debug('{0} :: {1}  - FINISHING.'
    .format(__name__, job_control.__name__))


def process_metrics(p, request_meta):
    """ Worker process for requests -
        this will typically operate in a forked process """

    logging.info(__name__ + ' :: START JOB\n\t%s (PID = %s)\n' % (str(request_meta),
                                                             getpid()))

    # obtain user list - handle the case where a lone user ID is passed
    if search(MW_UID_REGEX, str(request_meta.cohort_expr)):
        users = [request_meta.cohort_expr]
    # Special case where user lists are to be generated based on registered
    # user reg dates from the logging table -- see src/metrics/users.py
    elif request_meta.cohort_expr == 'all':
        users = MediaWikiUser(query_type=1)
    else:
        users = get_users(request_meta.cohort_expr)

    # process request
    results = process_data_request(request_meta, users)

    results = str(results)
    response_size = getsizeof(results, None)

    if response_size > MAX_BLOCK_SIZE:
        index = 0

        # Dump the data in pieces - block until it is picked up
        while index < response_size:
            p.put(results[index:index+MAX_BLOCK_SIZE], block=True)
            index += MAX_BLOCK_SIZE

    logging.info(__name__ + ' :: END JOB\n\t%s (PID = %s)\n' % (str(request_meta),
                                                                getpid()))



# REQUEST FLOW HANDLER
# ###################


from dateutil.parser import parse as date_parse
from copy import deepcopy

from user_metrics.etl.data_loader import DataLoader
import user_metrics.metrics.user_metric as um
import user_metrics.etl.time_series_process_methods as tspm
from user_metrics.api.engine.request_meta import ParameterMapping
from user_metrics.api.engine.response_meta import format_response
from user_metrics.api.engine import DATETIME_STR_FORMAT
from user_metrics.api.engine.request_meta import get_agg_key, \
    get_aggregator_type, request_types

INTERVALS_PER_THREAD = 10
MAX_THREADS = 5

USER_THREADS = settings.__user_thread_max__
REVISION_THREADS = settings.__rev_thread_max__
DEFAULT_INERVAL_LENGTH = 24

# create shorthand method refs
to_string = DataLoader().cast_elems_to_string

def process_data_request(request_meta, users):
    """
        Main entry point of the module, prepares results for a given request.
        Coordinates a request based on the following parameters::

            metric_handle (string) - determines the type of metric object to
            build.  Keys metric_dict.

            users (list) - list of user IDs.

            **kwargs - Keyword arguments may contain a variety of variables.
            Most notably, "aggregator" if the request requires aggregation,
            "time_series" flag indicating a time series request.  The
            remaining kwargs specify metric object parameters.
    """

    # Set interval length in hours if not present
    if not request_meta.interval:
        request_meta.interval = DEFAULT_INERVAL_LENGTH
    else:
        request_meta.interval = float(request_meta.interval)

    # Get the aggregator key
    agg_key = get_agg_key(request_meta.aggregator, request_meta.metric) if \
        request_meta.aggregator else None

    args = ParameterMapping.map(request_meta)

    # Initialize the results
    results, metric_class, metric_obj = format_response(request_meta)

    start = metric_obj.datetime_start
    end = metric_obj.datetime_end

    if results['type'] == request_types.time_series:

        # Get aggregator
        try:
            aggregator_func = get_aggregator_type(agg_key)
        except MetricsAPIError as e:
            results['data'] = 'Request failed. ' + e.message
            return results

        # Determine intervals and thread allocation
        total_intervals = (date_parse(end) - date_parse(start)).\
                          total_seconds() / (3600 * request_meta.interval)
        time_threads = max(1, int(total_intervals / INTERVALS_PER_THREAD))
        time_threads = min(MAX_THREADS, time_threads)

        logging.info(__name__ + ' :: Initiating time series for %(metric)s\n'
                                '\tAGGREGATOR = %(agg)s\n'
                                '\tFROM: %(start)s,\tTO: %(end)s.' %
                                {
                                    'metric': metric_class.__name__,
                                    'agg': request_meta.aggregator,
                                    'start': str(start),
                                    'end': str(end),
                                    })
        metric_threads = '"k_" : {0}, "kr_" : {1}'.format(USER_THREADS,
            REVISION_THREADS)
        metric_threads = '{' + metric_threads + '}'

        new_kwargs = deepcopy(args)

        del new_kwargs['interval']
        del new_kwargs['aggregator']
        del new_kwargs['datetime_start']
        del new_kwargs['datetime_end']

        out = tspm.build_time_series(start,
            end,
            request_meta.interval,
            metric_class,
            aggregator_func,
            users,
            kt_=time_threads,
            metric_threads=metric_threads,
            log=True,
            **new_kwargs)

        results['header'] = ['timestamp'] + \
                            getattr(aggregator_func,
                                    um.METRIC_AGG_METHOD_HEAD)
        for row in out:
            timestamp = date_parse(row[0][:19]).strftime(
                DATETIME_STR_FORMAT)
            results['data'][timestamp] = row[3:]

    elif results['type'] == request_types.aggregator:

        # Get aggregator
        try:
            aggregator_func = get_aggregator_type(agg_key)
        except MetricsAPIError as e:
            results['data'] = 'Request failed. ' + e.message
            return results

        logging.info(__name__ + ' :: Initiating aggregator for %(metric)s\n'
                                '\AGGREGATOR = %(agg)s\n'
                                '\tFROM: %(start)s,\tTO: %(end)s.' %
                                {
                                    'metric': metric_class.__name__,
                                    'agg': request_meta.aggregator,
                                    'start': str(start),
                                    'end': str(end),
                                    })
        metric_obj.process(users,
                           k_=USER_THREADS,
                           kr_=REVISION_THREADS,
                           log_=True,
                           **args)
        r = um.aggregator(aggregator_func, metric_obj, metric_obj.header())
        results['header'] = to_string(r.header)
        results['data'] = r.data[1:]

    elif results['type'] == request_types.raw:

        logging.info(__name__ + ':: Initiating raw request for %(metric)s\n'
                                '\tFROM: %(start)s,\tTO: %(end)s.' %
                                {
                                    'metric': metric_class.__name__,
                                    'start': str(start),
                                    'end': str(end),
                                    })
        metric_obj.process(users,
                           k_=USER_THREADS,
                           kr_=REVISION_THREADS,
                           log_=True,
                           **args)
        for m in metric_obj.__iter__():
            results['data'][m[0]] = m[1:]

    return results


