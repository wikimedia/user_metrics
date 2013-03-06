#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
    This module defines the entry point for flask_ web server implementation
    of the Wikimedia User Metrics API.  This module is consumable
    by the Apache web server via WSGI interface via mod_wsgi.  An Apache
    server can be pointed to api.wsgi such that Apache may be used as a
    wrapper in this way.

    .. _flask: http://flask.pocoo.org

    Cohort Data
    ^^^^^^^^^^^

    Cohort data is maintained in the host s1-analytics-slave.eqiad.wmnet under
    the `staging` database in the `usertags` and `usertags_meta` tables: ::

        +---------+-----------------+------+-----+---------+-------+
        | Field   | Type            | Null | Key | Default | Extra |
        +---------+-----------------+------+-----+---------+-------+
        | ut_user | int(5) unsigned | NO   | PRI | NULL    |       |
        | ut_tag  | int(4) unsigned | NO   | PRI | NULL    |       |
        +---------+-----------------+------+-----+---------+-------+

        +-------------+-----------------+------+-----+---------+
        | Field       | Type            | Null | Key | Default |
        +-------------+-----------------+------+-----+---------+
        | utm_id      | int(5) unsigned | NO   | PRI | NULL    |
        | utm_name    | varchar(255)    | NO   |     |         |
        | utm_notes   | varchar(255)    | YES  |     | NULL    |
        | utm_touched | datetime        | YES  |     | NULL    |
        +-------------+-----------------+------+-----+---------+


"""

__author__ = {
    "dario taraborelli": "dario@wikimedia.org",
    "ryan faulkner": "rfaulkner@wikimedia.org"
}
__date__ = "2012-12-21"
__license__ = "GPL (version 2 or later)"


import cPickle
import multiprocessing as mp
from datetime import datetime

from user_metrics.api import api_data
from user_metrics.config import logging, settings
from engine.request_manager import job_control
from user_metrics.api.views import app
from user_metrics.api.engine.request_meta import request_queue
from user_metrics.api.engine import DATETIME_STR_FORMAT


######
#
# Define Custom Classes
#
#######


class APIMethods(object):
    """ Provides initialization and boilerplate for API execution """

    __instance = None   # Singleton instance
    __job_controller_proc = None

    def __new__(cls):
        """ This class is Singleton, return only one instance """
        if not cls.__instance:
            cls.__instance = super(APIMethods, cls).__new__(cls)
        return cls.__instance

    def __init__(self):
        """ Load cached data from pickle file. """

        # Setup the job controller
        if not self.__job_controller_proc:
            self._setup_controller(request_queue)


    def close(self):
        """ When the instance is deleted store the pickled data and shutdown
            the job controller """

        # Handle persisting data to file
        pkl_file = None
        try:
            timestamp = datetime.now().strftime(DATETIME_STR_FORMAT)
            pkl_file = open(settings.__data_file_dir__ +
                            'api_data_{0}.pkl'.
                            format(timestamp), 'wb')
            cPickle.dump(api_data, pkl_file)
        except Exception:
            logging.error(__name__ + '::Could not pickle data.')
        finally:
            if hasattr(pkl_file, 'close'):
                pkl_file.close()

        # Try to shutdown the job control proc gracefully
        try:
            if self.__job_controller_proc and\
               hasattr(self.__job_controller_proc, 'is_alive') and\
               self.__job_controller_proc.is_alive():
                self.__job_controller_proc.terminate()
        except Exception:
            logging.error(__name__ + ' :: Could not shut down controller.')

    def _setup_controller(self, req_queue):
        """
            Sets up the process that handles API jobs
        """
        self.__job_controller_proc = mp.Process(target=job_control,
                                                args=(req_queue,))
        if not self.__job_controller_proc.is_alive():
            self.__job_controller_proc.start()


######
#
# Execution
#
#######


if __name__ == '__main__':

    # initialize API data - get the instance
    a = APIMethods()
    try:
        app.run(debug=True)
    finally:
        a.close()
