# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright (c) 2015, Battelle Memorial Institute
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
# ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# The views and conclusions contained in the software and documentation are those
# of the authors and should not be interpreted as representing official policies,
# either expressed or implied, of the FreeBSD Project.
#

# This material was prepared as an account of work sponsored by an
# agency of the United States Government.  Neither the United States
# Government nor the United States Department of Energy, nor Battelle,
# nor any of their employees, nor any jurisdiction or organization
# that has cooperated in the development of these materials, makes
# any warranty, express or implied, or assumes any legal liability
# or responsibility for the accuracy, completeness, or usefulness or
# any information, apparatus, product, software, or process disclosed,
# or represents that its use would not infringe privately owned rights.
#
# Reference herein to any specific commercial product, process, or
# service by trade name, trademark, manufacturer, or otherwise does
# not necessarily constitute or imply its endorsement, recommendation,
# r favoring by the United States Government or any agency thereof,
# or Battelle Memorial Institute. The views and opinions of authors
# expressed herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY
# operated by BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830

#}}}

from __future__ import absolute_import

import logging
import sys
import gevent
import datetime
import time

from volttron.platform.jsonrpc import RemoteError
from volttron.platform.vip.connection import Connection
from volttron.platform.vip.agent import Agent, Core, PubSub, Unreachable
from volttron.platform.agent import utils
from volttron.platform.agent.known_identities import CONTROL

utils.setup_logging()
_log = logging.getLogger()


class FailoverAgent(Agent):

    def __init__(self, config_path, **kwargs):
        super(FailoverAgent, self).__init__(**kwargs)
        config = utils.load_config(config_path)

        # Config file options
        self.agent_id = config["agent_id"]
        self.remote_id = config["remote_id"]
        self.remote_vip = config["remote_vip"]
        self.remote_serverkey = config["remote_serverkey"]

        self.agent_vip_identity = config["agent_vip_identity"]
        self.heartbeat_period = config["heartbeat_period"]
        self.timeout = config["timeout"]

        self.vc_timeout = 0
        self.remote_timeout = 0
        self.agent_uuid = None
        self.heartbeat = None
        self.last_connected = None

        self._state = False, False
        self._state_machine = getattr(self, self.agent_id + '_state_machine')

    @Core.receiver("onstart")
    def onstart(self, sender, **kwargs):
        # Figure out the uuid to start and stop by VIP identity
        agents = self.vip.rpc.call(CONTROL, 'list_agents').get()
        uuids = [a['uuid'] for a in agents]
        for uuid in uuids:
            vip_id = self.vip.rpc.call(CONTROL,
                                       'agent_vip_identity',
                                       uuid).get()

            if vip_id == self.agent_vip_identity:
                self.agent_uuid = uuid

        # We won't be able to do anything with an agent that isn't installed
        # sys.exit() ?
        if self.agent_uuid is None:
            _log.error("Agent {} is not installed"
                       .format(self.agent_vip_identity))

        # Start an agent to send heartbeats to the other failover instance
        self.heartbeat = self.build_connection()

        connected = self.heartbeat.is_connected()
        _log.debug("is connected to remote instance: {}".format(connected))

        def periodic():
            try:
                self.heartbeat.publish('heartbeat/{}'.format(self.agent_id))
                self.last_connected = self.timestamp()
            except Unreachable:
                if self.timestamp() < self.last_connected + self.timeout:
                    _log.debug("Attempting reconnect to remote instance")
                    self.heartbeat.kill()
                    self.heartbeat = self.build_connection()
                    self.last_connected = self.timestamp()

        self.core.periodic(self.heartbeat_period, periodic)

    def timestamp(self):
        return time.mktime(datetime.datetime.now().timetuple())

    def build_connection(self):
        return Connection(self.remote_vip,
                          peer=self.agent_vip_identity,
                          serverkey=self.remote_serverkey,
                          publickey=self.core.publickey,
                          secretkey=self.core.secretkey)

    @PubSub.subscribe('pubsub', 'heartbeat')
    def on_match(self, peer, sender, bus, topic, headers, message):
        if topic.startswith('heartbeat/VolttronCentralAgent'):
            self.vc_timeout = self.timeout
        elif topic.startswith('heartbeat/' + self.remote_id):
            self.remote_timeout = self.timeout

    @Core.periodic(1)
    def check_pulse(self):
        self.vc_timeout -= 1
        self.remote_timeout -= 1

        vc_is_up = self.vc_timeout > 0
        remote_is_up = self.remote_timeout > 0
        current_state = remote_is_up, vc_is_up

        self._state_machine(current_state)

    def _agent_control(self, command):
        try:
            self.vip.rpc.call(CONTROL, command, self.agent_uuid).get()
        except RemoteError as e:
            _log.error("Error calling {} on control".format(command))

    def primary_state_machine(self, current_state):
        raise NotImplementedError("Coordination with VC not implemeted")

        secondary_is_up, vc_is_up = current_state
        if secondary_is_up or vc_is_up:
            self._agent_control('start_agent')
        else:
            self._agent_control('stop_agent')

    def secondary_state_machine(self, current_state):
        raise NotImplementedError("Coordination with VC not implemeted")

        primary_is_up, vc_is_up = current_state
        if not primary_is_up and vc_is_up:
            pass # verify and start master
        else:
            self._agent_control('stop_agent')

    def simple_primary_state_machine(self, current_state):
        if current_state != self._state:
            self._state = current_state
            _log.warn('Starting agent {}'.format(self.agent_vip_identity))

        proc_info = self.vip.rpc.call(CONTROL,
                                      'agent_status',
                                      self.agent_uuid).get()

        is_running = proc_info[0] > 0 and proc_info[1] == None
        if not is_running:
            self._agent_control('start_agent')

    def simple_secondary_state_machine(self, current_state):
        primary_is_up, _ = current_state

        if primary_is_up:
            if current_state != self._state:
                self._state = current_state
                _log.warn('Primary is active stopping agent {}'
                          .format(self.agent_vip_identity))
            self._agent_control('stop_agent')

        else:
            if current_state != self._state:
                self._state = current_state
                _log.warn('Primary is inactive starting agent {}'
                          .format(self.agent_vip_identity))
            proc_info = self.vip.rpc.call(CONTROL,
                                          'agent_status',
                                          self.agent_uuid).get()
            is_running = proc_info[0] > 0 and proc_info[1] == None
            if not is_running:
                self._agent_control('start_agent')


def main():
    try:
        utils.vip_main(FailoverAgent)
    except KeyboardInterrupt:
        pass


if __name__ == '__main__':
    main()