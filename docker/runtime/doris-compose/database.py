# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import cluster as CLUSTER
import os.path
import pymysql
import time
import utils
import uuid

LOG = utils.get_logger()


class FEState(object):

    def __init__(self, id, ip, is_master, alive, last_heartbeat, err_msg, role,
                 query_port, rpc_port, http_port, edit_log_port):
        self.id = id
        self.ip = ip
        self.is_master = is_master
        self.alive = alive
        self.last_heartbeat = last_heartbeat
        self.err_msg = err_msg
        self.role = role
        self.query_port = query_port
        self.rpc_port = rpc_port
        self.http_port = http_port
        self.edit_log_port = edit_log_port


class BEState(object):

    def __init__(self, id, ip, backend_id, decommissioned, alive, tablet_num,
                 last_heartbeat, err_msg, http_port, heartbeat_service_port):
        self.id = id
        self.ip = ip
        self.backend_id = backend_id
        self.decommissioned = decommissioned
        self.alive = alive
        self.tablet_num = tablet_num
        self.last_heartbeat = last_heartbeat
        self.err_msg = err_msg
        self.http_port = http_port
        self.heartbeat_service_port = heartbeat_service_port


class DBManager(object):

    def __init__(self, all_node_net_infos):
        self.all_node_net_infos = all_node_net_infos
        self.fe_states = {}
        self.be_states = {}
        self.conn = None
        self.fe_ip = ""
        self.fe_port = -1

    def get_all_fe(self):
        return self.fe_states

    def get_fe(self, id):
        return self.fe_states.get(id, None)

    def get_be(self, id):
        return self.be_states.get(id, None)

    def load_states(self):
        self._load_fe_states()
        self._load_be_states()

    def add_fe(self, fe_endpoint, role):
        try:
            sql = f"ALTER SYSTEM ADD {role} '{fe_endpoint}'"
            self._exec_query(sql)
            LOG.info(f"Added {role} FE {fe_endpoint} via SQL successfully.")
        except Exception as e:
            LOG.error(f"Failed to add FE {fe_endpoint} via SQL: {str(e)}")
            raise

    def drop_fe(self, fe_endpoint):
        id = CLUSTER.Node.get_id_from_ip(fe_endpoint[:fe_endpoint.find(":")])
        try:
            role = self.get_fe(id).role if self.get_fe(id) else "FOLLOWER"
            self._exec_query("ALTER SYSTEM DROP {} '{}'".format(
                role, fe_endpoint))
            LOG.info("Drop fe {} with id {} from db succ.".format(
                fe_endpoint, id))
        except Exception as e:
            if str(e).find("frontend does not exist") >= 0:
                LOG.info(
                    "Drop fe {} with id {} from db succ cause it does not exist in db."
                    .format(fe_endpoint, id))
                return
            raise e

    def add_be(self, be_endpoint):
        try:
            sql = f"ALTER SYSTEM ADD BACKEND '{be_endpoint}'"
            self._exec_query(sql)
            LOG.info(f"Added BE {be_endpoint} via SQL successfully.")
        except Exception as e:
            LOG.error(f"Failed to add BE {be_endpoint} via SQL: {str(e)}")
            raise

    def drop_be(self, be_endpoint):
        id = CLUSTER.Node.get_id_from_ip(be_endpoint[:be_endpoint.find(":")])
        try:
            self._exec_query(
                "ALTER SYSTEM DROPP BACKEND '{}'".format(be_endpoint))
            LOG.info("Drop be {} with id {} from db succ.".format(
                be_endpoint, id))
        except Exception as e:
            if str(e).find("backend does not exists") >= 0:
                LOG.info(
                    "Drop be {} with id {} from db succ cause it does not exist in db."
                    .format(be_endpoint, id))
                return
            raise e

    def decommission_be(self, be_endpoint):
        old_tablet_num = 0
        id = CLUSTER.Node.get_id_from_ip(be_endpoint[:be_endpoint.find(":")])
        start_ts = time.time()
        if id not in self.be_states:
            self._load_be_states()
        if id in self.be_states:
            be = self.be_states[id]
            old_tablet_num = be.tablet_num
            if not be.alive:
                raise Exception("Decommission be {} with id {} fail " \
                        "cause it's not alive, maybe you should specific --drop-force " \
                        " to dropp it from db".format(be_endpoint, id))
        try:
            self._exec_query(
                "ALTER SYSTEM DECOMMISSION BACKEND '{}'".format(be_endpoint))
            LOG.info("Mark be {} with id {} as decommissioned, start migrate its tablets, " \
                    "wait migrating job finish.".format(be_endpoint, id))
        except Exception as e:
            if str(e).find("Backend does not exist") >= 0:
                LOG.info("Decommission be {} with id {} from db succ " \
                        "cause it does not exist in db.".format(be_endpoint, id))
                return
            raise e

        while True:
            self._load_be_states()
            be = self.be_states.get(id, None)
            if not be:
                LOG.info("Decommission be {} succ, total migrate {} tablets, " \
                        "has drop it from db.".format(be_endpoint, old_tablet_num))
                return
            LOG.info(
                    "Decommission be {} status: alive {}, decommissioned {}. " \
                    "It is migrating its tablets, left {}/{} tablets. Time elapse {} s."
                .format(be_endpoint, be.alive, be.decommissioned, be.tablet_num, old_tablet_num,
                        int(time.time() - start_ts)))

            time.sleep(1)

    def create_default_storage_vault(self, cloud_store_config):
        try:
            # Create storage vault
            create_vault_sql = f"""
            CREATE STORAGE VAULT IF NOT EXISTS default_vault
            PROPERTIES (
                "type" = "S3",
                "s3.access_key" = "{cloud_store_config['DORIS_CLOUD_AK']}",
                "s3.secret_key" = "{cloud_store_config['DORIS_CLOUD_SK']}",
                "s3.endpoint" = "{cloud_store_config['DORIS_CLOUD_ENDPOINT']}",
                "s3.bucket" = "{cloud_store_config['DORIS_CLOUD_BUCKET']}",
                "s3.region" = "{cloud_store_config['DORIS_CLOUD_REGION']}",
                "s3.root.path" = "{str(uuid.uuid4())}",
                "provider" = "{cloud_store_config['DORIS_CLOUD_PROVIDER']}"
            );
            """
            # create hk storage vault from beijing cost 14s
            self._reset_conn(read_timeout=20)
            self._exec_query(create_vault_sql)
            LOG.info("Created storage vault 'default_vault'")

            # Set as default storage vault
            set_default_vault_sql = "SET default_vault as DEFAULT STORAGE VAULT;"
            self._exec_query(set_default_vault_sql)
            LOG.info("Set 'default_vault' as the default storage vault")

        except Exception as e:
            LOG.error(f"Failed to create default storage vault: {str(e)}")
            raise

    def _load_fe_states(self):
        fe_states = {}
        alive_master_fe_ip = None
        alive_master_fe_port = None
        for record in self._exec_query("show frontends"):
            name = record["Name"]
            ip = record["Host"]
            role = record["Role"]
            is_master = utils.is_true(record["IsMaster"])
            alive = utils.is_true(record["Alive"])
            query_port = int(record["QueryPort"])
            http_port = int(record["HttpPort"])
            rpc_port = int(record["RpcPort"])
            edit_log_port = int(record["EditLogPort"])
            id = None
            for net_info in self.all_node_net_infos:
                if net_info.ip == ip and net_info.ports.get("query_port",
                                                            -1) == query_port:
                    id = net_info.id
                    break
            if not id:
                id = CLUSTER.Node.get_id_from_ip(ip)
            last_heartbeat = utils.escape_null(record["LastHeartbeat"])
            err_msg = record["ErrMsg"]
            fe = FEState(id, ip, is_master, alive, last_heartbeat, err_msg,
                         role, query_port, rpc_port, http_port, edit_log_port)
            fe_states[id] = fe
            if is_master and alive:
                alive_master_fe_ip = ip
                alive_master_fe_port = query_port
            LOG.debug(
                "record of show frontends, name {}, ip {}, port {}, alive {}, is_master {}, role {}"
                .format(name, ip, query_port, alive, is_master, role))

        self.fe_states = fe_states
        if alive_master_fe_ip and (alive_master_fe_ip != self.fe_ip
                                   or alive_master_fe_port != self.fe_port):
            self.fe_ip = alive_master_fe_ip
            self.fe_port = alive_master_fe_port
            self._reset_conn()

    def _load_be_states(self):
        be_states = {}
        for record in self._exec_query("show backends"):
            backend_id = int(record["BackendId"])
            alive = utils.is_true(record["Alive"])
            decommissioned = utils.is_true(record["SystemDecommissioned"])
            tablet_num = int(record["TabletNum"])
            ip = record["Host"]
            heartbeat_service_port = int(record["HeartbeatPort"])
            http_port = int(record["HttpPort"])
            id = None
            for net_info in self.all_node_net_infos:
                if net_info.ip == ip and net_info.ports.get(
                        "heartbeat_service_port",
                        -1) == heartbeat_service_port:
                    id = net_info.id
                    break
            if not id:
                id = CLUSTER.Node.get_id_from_ip(ip)
            last_heartbeat = utils.escape_null(record["LastHeartbeat"])
            err_msg = record["ErrMsg"]
            be = BEState(id, ip, backend_id, decommissioned, alive, tablet_num,
                         last_heartbeat, err_msg, http_port,
                         heartbeat_service_port)
            be_states[id] = be
        self.be_states = be_states

    # return rows, and each row is a record map
    def _exec_query(self, sql, retries=3):
        self._prepare_conn()
        for attempt in range(retries):
            try:
                with self.conn.cursor() as cursor:
                    cursor.execute(sql)
                    fields = [field_md[0] for field_md in cursor.description
                              ] if cursor.description else []
                    return [
                        dict(zip(fields, row)) for row in cursor.fetchall()
                    ]
            except Exception as e:
                LOG.warning(
                    f"Error occurred: fe {self.fe_ip}:{self.fe_port}, sql `{sql}`, err {e}"
                )
                if "timed out" in str(e).lower() and attempt < retries - 1:
                    LOG.warning(
                        f"Query timed out, fe {self.fe_ip}:{self.fe_port}. Retrying {attempt + 1}/{retries}..."
                    )
                    self._reset_conn()
                else:
                    raise e
        raise Exception("Max retries exceeded")

    def _prepare_conn(self):
        if self.conn:
            return
        self._reset_conn()

    def _reset_conn(self, read_timeout=10, connect_timeout=3):
        self.conn = pymysql.connect(user="root",
                                    host=self.fe_ip,
                                    read_timeout=read_timeout,
                                    connect_timeout=connect_timeout,
                                    port=self.fe_port)


def get_db_mgr(cluster_name, all_node_net_infos, required_load_succ=True):
    assert cluster_name
    db_mgr = DBManager(all_node_net_infos)
    master_fe_query_addr_file = CLUSTER.get_master_fe_addr_path(cluster_name)
    master_fe_query_addr = None
    if os.path.exists(master_fe_query_addr_file):
        with open(master_fe_query_addr_file, "r") as f:
            master_fe_query_addr = f.read().strip()

    if not master_fe_query_addr:
        return db_mgr

    pos = master_fe_query_addr.find(':')
    master_fe_ip = master_fe_query_addr[:pos]
    master_fe_port = int(master_fe_query_addr[pos + 1:])

    chose_fe_ip = ""
    chose_fe_port = -1
    alive_fe = None
    cluster = CLUSTER.Cluster.load(cluster_name)
    containers = utils.get_doris_containers(cluster_name).get(cluster_name, [])
    for container in containers:
        if utils.is_container_running(container):
            _, node_type, id = utils.parse_service_name(container.name)
            if node_type == CLUSTER.Node.TYPE_FE:
                node = cluster.get_node(node_type, id)
                if not alive_fe:
                    alive_fe = node
                if node.get_ip() == master_fe_ip and node.meta["ports"][
                        "query_port"] == master_fe_port:
                    alive_fe = node
                    break
    if alive_fe:
        chose_fe_ip = alive_fe.get_ip()
        chose_fe_port = alive_fe.meta["ports"]["query_port"]
    elif utils.is_socket_avail(master_fe_ip, master_fe_port):
        chose_fe_ip = master_fe_ip
        chose_fe_port = master_fe_port
    else:
        LOG.debug("no available alive fe")
        return db_mgr

    db_mgr.fe_ip = chose_fe_ip
    db_mgr.fe_port = chose_fe_port
    try:
        db_mgr.load_states()
    except Exception as e:
        if required_load_succ:
            raise e
        #LOG.exception(e)

    return db_mgr
