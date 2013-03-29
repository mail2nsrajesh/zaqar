# Copyright (c) 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import testtools

from marconi.storage import exceptions
from marconi.storage import sqlite
from marconi.tests import util as testing


#TODO(zyuan): let tests/storage/base.py handle these
class TestSqlite(testing.TestBase):

    def setUp(self):
        super(TestSqlite, self).setUp()

        storage = sqlite.Driver()
        self.queue_ctrl = storage.queue_controller
        self.queue_ctrl.upsert('fizbit', {'_message_ttl': 40}, '480924')
        self.msg_ctrl = storage.message_controller

    def test_some_messages(self):
        doc = [
            {
                'body': {
                    'event': 'BackupStarted',
                    'backupId': 'c378813c-3f0b-11e2-ad92-7823d2b0f3ce',
                },
                'ttl': 30,
            },
        ]

        for _ in range(10):
            self.msg_ctrl.post('fizbit', doc, '480924',
                               client_uuid='30387f00')
        msgid = self.msg_ctrl.post('fizbit', doc, '480924',
                                   client_uuid='79ed56f8')[0]

        self.assertEquals(
            self.queue_ctrl.stats('fizbit', '480924')['messages'], 11)

        msgs = list(self.msg_ctrl.list('fizbit', '480924',
                                       client_uuid='30387f00'))

        self.assertEquals(len(msgs), 1)

        #TODO(zyuan): move this to tests/storage/test_impl_sqlite.py
        msgs = list(self.msg_ctrl.list('fizbit', '480924',
                                       marker='illformed'))

        self.assertEquals(len(msgs), 0)

        cnt = 0
        marker = None
        while True:
            nomsg = True
            for msg in self.msg_ctrl.list('fizbit', '480924',
                                          limit=3, marker=marker,
                                          client_uuid='79ed56f8'):
                nomsg = False
            if nomsg:
                break
            marker = msg['marker']
            cnt += 1

        self.assertEquals(cnt, 4)

        self.assertIn(
            'body', self.msg_ctrl.get('fizbit', msgid, '480924'))

        self.msg_ctrl.delete('fizbit', msgid, '480924')

        with testtools.ExpectedException(exceptions.DoesNotExist):
            self.msg_ctrl.get('fizbit', msgid, '480924')

    def test_expired_messages(self):
        doc = [
            {'body': {}, 'ttl': 0},
        ]

        msgid = self.msg_ctrl.post('fizbit', doc, '480924',
                                   client_uuid='unused')[0]

        with testtools.ExpectedException(exceptions.DoesNotExist):
            self.msg_ctrl.get('fizbit', msgid, '480924')

    def test_nonexsitent(self):
        with testtools.ExpectedException(exceptions.DoesNotExist):
            self.msg_ctrl.post('nonexistent', [], '480924',
                               client_uuid='30387f00')

        with testtools.ExpectedException(exceptions.DoesNotExist):
            for _ in self.msg_ctrl.list('nonexistent', '480924'):
                pass

        with testtools.ExpectedException(exceptions.DoesNotExist):
            self.queue_ctrl.stats('nonexistent', '480924')

    #TODO(zyuan): move this to tests/storage/test_impl_sqlite.py
    def test_illformed_id(self):

        # SQlite-specific tests.  Since all IDs exposed in APIs are opaque,
        # any ill-formed IDs should be regarded as non-existing ones.

        with testtools.ExpectedException(exceptions.DoesNotExist):
            self.msg_ctrl.get('nonexistent', 'illformed', '480924')

        self.msg_ctrl.delete('nonexistent', 'illformed', '480924')

    def tearDown(self):
        self.queue_ctrl.delete('fizbit', '480924')

        super(TestSqlite, self).tearDown()