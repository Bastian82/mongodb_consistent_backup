import logging

from time import sleep

from mongodb_consistent_backup.Common import DB, MongoUri, validate_hostname
from mongodb_consistent_backup.Errors import DBOperationError, Error, OperationError
from mongodb_consistent_backup.Replication import Replset


class Sharding:
    def __init__(self, config, timer, db):
        self.config             = config
        self.timer              = timer
        self.db                 = db
        self.balancer_wait_secs = self.config.sharding.balancer.wait_secs
        self.balancer_sleep     = self.config.sharding.balancer.ping_secs

        self.timer_name            = self.__class__.__name__
        self.config_server         = None
        self.config_db             = None
        self._balancer_state_start = None
        self.restored              = False

        # Get a DB connection
        try:
            if isinstance(self.db, DB):
                self.connection = self.db.connection()
                if not self.db.is_mongos() and not self.db.is_configsvr():
                    raise DBOperationError('MongoDB connection is not to a mongos or configsvr!')
            else:
                raise Error("'db' field is not an instance of class: 'DB'!")
        except Exception, e:
            logging.fatal("Could not get DB connection! Error: %s" % e)
            raise DBOperationError(e)

    def close(self):
        if self.config_db:
            self.config_db.close()
        return self.restore_balancer_state()

    def get_start_state(self):
        self._balancer_state_start = self.get_balancer_state()
        logging.info("Began with balancer state running: %s" % str(self._balancer_state_start))
        return self._balancer_state_start

    def shards(self):
        try:
            if self.db.is_configsvr() and self.db.server_version() < tuple("3.4.0".split(".")):
                return self.connection['config'].shards.find()
            else:
                listShards = self.db.admin_command("listShards")
                if 'shards' in listShards:
                    return listShards['shards']
        except Exception, e:
            raise DBOperationError(e)

    def check_balancer_running(self):
        try:
            config = self.connection['config']
            lock   = config['locks'].find_one({'_id': 'balancer'})
            if 'state' in lock and int(lock['state']) == 0:
                return False
            return True
        except Exception, e:
            raise DBOperationError(e)

    def get_balancer_state(self):
        try:
            config = self.connection['config']
            state  = config['settings'].find_one({'_id': 'balancer'})

            if not state:
               return True
            elif 'stopped' in state and state.get('stopped') is True:
               return False
            else:
               return True
        except Exception, e:
            raise DBOperationError(e)

    def set_balancer(self, value):
        try:
            if value is True:
                set_value = False
            elif value is False:
                set_value = True
            else:
                set_value = True
            config = self.connection['config']
            config['settings'].update_one({'_id': 'balancer'}, {'$set': {'stopped': set_value}})
        except Exception, e:
            logging.fatal("Failed to set balancer state! Error: %s" % e)
            raise DBOperationError(e)

    def restore_balancer_state(self):
        if self._balancer_state_start is not None and not self.restored:
            try:
                logging.info("Restoring balancer state to: %s" % str(self._balancer_state_start))
                self.set_balancer(self._balancer_state_start)
                self.restored = True
            except Exception, e:
                logging.fatal("Failed to set balancer state! Error: %s" % e)
                raise DBOperationError(e)

    def stop_balancer(self):
        logging.info("Stopping the balancer and waiting a max of %i sec" % self.balancer_wait_secs)
        wait_cnt = 0
        self.timer.start(self.timer_name)
        self.set_balancer(False)
        while wait_cnt < self.balancer_wait_secs:
            if self.check_balancer_running():
                wait_cnt += self.balancer_sleep
                logging.info("Balancer is still running, sleeping for %i sec(s)" % self.balancer_sleep)
                sleep(self.balancer_sleep)
            else:
                self.timer.stop(self.timer_name)
                logging.info("Balancer stopped after %.2f seconds" % self.timer.duration(self.timer_name))
                return
        logging.fatal("Could not stop balancer %s: %s!" % (self.db.uri, e))
        raise DBOperationError("Could not stop balancer %s: %s" % (self.db.uri, e))

    def get_configdb_hosts(self):
        try:
            cmdlineopts = self.db.admin_command("getCmdLineOpts")
            config_string = None
            if cmdlineopts.get('parsed').get('configdb'):
                config_string = cmdlineopts.get('parsed').get('configdb')
            elif cmdlineopts.get('parsed').get('sharding').get('configDB'):
                config_string = cmdlineopts.get('parsed').get('sharding').get('configDB')

            if config_string:
                return MongoUri(config_string, 27019)
            elif self.db.is_configsvr():
                return self.db.uri
            else:
                logging.fatal("Unable to locate config servers for %s!" % self.db.uri)
                raise OperationError("Unable to locate config servers for %s!" % self.db.uri)
        except Exception, e:
            raise OperationError(e)

    def get_config_server(self, force=False):
        if force or not self.config_server:
            configdb_uri = self.get_configdb_hosts()
            try:
                logging.info("Found sharding config server: %s" % configdb_uri)
                if self.db.uri.hosts() == configdb_uri.hosts():
                    self.config_db = self.db
                    logging.debug("Re-using seed connection to config server(s)")
                else:
                    self.config_db = DB(configdb_uri, self.config, True, 'secondaryPreferred')
                if self.config_db.is_replset():
                    self.config_server = Replset(self.config, self.config_db) 
                else:
                    self.config_server = { 'host': configdb_uri.hosts() }
                    self.config_db.close()
            except Exception, e:
                logging.fatal("Unable to locate config servers using %s: %s!" % (self.db.uri, e))
                raise OperationError(e)
        return self.config_server
