# Copyright (c) 2014 Prashanth Raghu.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import functools

import msgpack
import redis

from zaqar.common import decorators
from zaqar.openstack.common import log as logging
from zaqar.openstack.common import timeutils
from zaqar.queues import storage
from zaqar.queues.storage import errors
from zaqar.queues.storage.redis import messages
from zaqar.queues.storage.redis import utils

LOG = logging.getLogger(__name__)

QUEUE_CLAIMS_SUFFIX = 'claims'
CLAIM_MESSAGES_SUFFIX = 'messages'

RETRY_CLAIM_TIMEOUT = 10


class ClaimController(storage.Claim):
    """Implements claim resource operations using Redis.

    Redis Data Structures:
    ----------------------
    Claims list (Redis set) contains claim ids

    Key: <project-id_q-name>

        Name                Field
        -------------------------
        claim_ids               m

    Claimed Messages (Redis set) contains the list of
    message ids stored per claim

    Key: <claim_id>_messages

    Claim info(Redis Hash):

    Key: <claim_id>

        Name                Field
        -------------------------
        ttl             ->     t
        id              ->     id
        expires         ->     e
    """
    def __init__(self, *args, **kwargs):
        super(ClaimController, self).__init__(*args, **kwargs)
        self._client = self.driver.connection

        self._packer = msgpack.Packer(encoding='utf-8',
                                      use_bin_type=True).pack
        self._unpacker = functools.partial(msgpack.unpackb, encoding='utf-8')

    def _get_claim_info(self, claim_id, fields, transform=int):
        """Get one or more fields from the claim Info."""

        values = self._client.hmget(claim_id, fields)
        return [transform(v) for v in values] if transform else values

    def _exists(self, queue, claim_id, project):
        client = self._client
        claims_set_key = utils.scope_claims_set(queue, project,
                                                QUEUE_CLAIMS_SUFFIX)

        # Return False if no such claim exists
        # TODO(prashanthr_): Discuss the feasibility of a bloom filter.
        if not client.sismember(claims_set_key, claim_id):
            return False

        expires = self._get_claim_info(claim_id, b'e')[0]
        now = timeutils.utcnow_ts()

        if now > expires:
            return False

        return True

    def _get_claimed_message_keys(self, claim_id):
        return self._client.lrange(claim_id, 0, -1)

    @decorators.lazy_property(write=False)
    def _message_ctrl(self):
        return self.driver.message_controller

    @decorators.lazy_property(write=False)
    def _queue_ctrl(self):
        return self.driver.queue_controller

    @utils.raises_conn_error
    @utils.retries_on_connection_error
    def get(self, queue, claim_id, project=None):
        if not self._exists(queue, claim_id, project):
            raise errors.ClaimDoesNotExist(queue, project, claim_id)

        claim_msgs_key = utils.scope_claim_messages(claim_id,
                                                    CLAIM_MESSAGES_SUFFIX)

        # basic_messages
        msg_keys = self._get_claimed_message_keys(claim_msgs_key)

        with self._client.pipeline() as pipe:
            for key in msg_keys:
                pipe.hgetall(key)

            raw_messages = pipe.execute()

        now = timeutils.utcnow_ts()
        basic_messages = [messages.Message.from_redis(msg).to_basic(now)
                          for msg in raw_messages if msg]

        # claim_meta
        now = timeutils.utcnow_ts()
        expires, ttl = self._get_claim_info(claim_id, [b'e', b't'])
        update_time = expires - ttl
        age = now - update_time

        claim_meta = {
            'age': age,
            'ttl': ttl,
            'id': claim_id,
        }

        return claim_meta, basic_messages

    @utils.raises_conn_error
    @utils.retries_on_connection_error
    def create(self, queue, metadata, project=None,
               limit=storage.DEFAULT_MESSAGES_PER_CLAIM):

        ttl = int(metadata.get('ttl', 60))
        grace = int(metadata.get('grace', 60))
        msg_ttl = ttl + grace

        claim_id = utils.generate_uuid()
        claim_key = utils.scope_claim_messages(claim_id,
                                               CLAIM_MESSAGES_SUFFIX)

        claims_set_key = utils.scope_claims_set(queue, project,
                                                QUEUE_CLAIMS_SUFFIX)

        counter_key = self._queue_ctrl._claim_counter_key(queue, project)

        with self._client.pipeline() as pipe:

            start_ts = timeutils.utcnow_ts()

            # NOTE(kgriffs): Retry the operation if another transaction
            # completes before this one, in which case it will have
            # claimed the same messages the current thread is trying
            # to claim, and therefoe we must try for another batch.
            #
            # This loop will eventually time out if we can't manage to
            # claim any messages due to other threads continually beating
            # us to the punch.

            # TODO(kgriffs): Would it be beneficial (or harmful) to
            # introducce a backoff sleep in between retries?
            while (timeutils.utcnow_ts() - start_ts) < RETRY_CLAIM_TIMEOUT:

                # NOTE(kgriffs): The algorithm for claiming messages:
                #
                # 1. Get a batch of messages that are currently active.
                # 2. For each active message in the batch, extend its
                #    lifetime IFF it would otherwise expire before the
                #    claim itself does.
                # 3. Associate the claim with each message
                # 4. Create a claim record with details such as TTL
                #    and expiration time.
                # 5. Add the claim's ID to a set to facilitate fast
                #    existence checks.

                results = self._message_ctrl._active(queue, project=project,
                                                     limit=limit)

                cursor = next(results)
                msg_list = list(cursor)

                # NOTE(kgriffs): If there are no active messages to
                # claim, simply return an empty list.
                if not msg_list:
                    return (None, iter([]))

                basic_messages = []

                try:
                    # TODO(kgriffs): Is it faster/better to do this all
                    # in a Lua script instead of using an app-layer
                    # transaction?

                    # NOTE(kgriffs): Abort the entire transaction if
                    # another request beats us to the punch. We detect
                    # this by putting a watch on the key that will have
                    # one of its fields updated as the final step of
                    # the transaction.
                    pipe.watch(counter_key)
                    pipe.multi()

                    now = timeutils.utcnow_ts()

                    claim_expires = now + ttl
                    msg_expires = claim_expires + grace

                    # Associate the claim with each message
                    for msg in msg_list:
                        msg.claim_id = claim_id
                        msg.claim_expires = claim_expires

                        if _msg_would_expire(msg, msg_expires):
                            msg.ttl = msg_ttl
                            msg.expires = msg_expires

                        pipe.rpush(claim_key, msg.id)

                        # TODO(kgriffs): Rather than writing back the
                        # entire message, only set the fields that
                        # have changed.
                        msg.to_redis(pipe)

                        basic_messages.append(msg.to_basic(now))

                    # Create the claim
                    claim_info = {
                        'id': claim_id,
                        't': ttl,
                        'e': claim_expires
                    }

                    pipe.hmset(claim_id, claim_info)

                    # NOTE(kgriffs): Add the claim ID to a set so that
                    # existence checks can be performed quickly.
                    pipe.sadd(claims_set_key, claim_id)

                    # NOTE(kgriffs): Update a counter that facilitates
                    # the queue stats calculation.
                    self._queue_ctrl._inc_claimed(queue, project,
                                                  len(msg_list),
                                                  pipe=pipe)

                    pipe.execute()
                    return claim_id, basic_messages

                except redis.exceptions.WatchError:
                    continue

        raise errors.ClaimConflict(queue, project)

    @utils.raises_conn_error
    @utils.retries_on_connection_error
    def update(self, queue, claim_id, metadata, project=None):
        if not self._exists(queue, claim_id, project):
            raise errors.ClaimDoesNotExist(claim_id, queue, project)

        now = timeutils.utcnow_ts()

        claim_ttl = int(metadata.get('ttl', 60))
        claim_expires = now + claim_ttl

        grace = int(metadata.get('grace', 60))
        msg_ttl = claim_ttl + grace
        msg_expires = claim_expires + grace

        claim_messages = utils.scope_claim_messages(claim_id,
                                                    CLAIM_MESSAGES_SUFFIX)

        msg_keys = self._get_claimed_message_keys(claim_messages)

        with self._client.pipeline() as pipe:
            for key in msg_keys:
                pipe.hgetall(key)

            claimed_msgs = pipe.execute()

        claim_info = {
            't': claim_ttl,
            'e': claim_expires,
        }

        with self._client.pipeline() as pipe:
            for msg in claimed_msgs:
                if msg:
                    msg = messages.Message.from_redis(msg)
                    msg.claim_id = claim_id
                    msg.claim_expires = claim_expires

                    if _msg_would_expire(msg, msg_expires):
                        msg.ttl = msg_ttl
                        msg.expires = msg_expires

                    # TODO(kgriffs): Rather than writing back the
                    # entire message, only set the fields that
                    # have changed.
                    msg.to_redis(pipe)

            # Update the claim id and claim expiration info
            # for all the messages.
            pipe.hmset(claim_id, claim_info)

            pipe.execute()

    @utils.raises_conn_error
    @utils.retries_on_connection_error
    def delete(self, queue, claim_id, project=None):
        # NOTE(prashanthr_): Return silently when the claim
        # does not exist
        if not self._exists(queue, claim_id, project):
            return

        now = timeutils.utcnow_ts()
        claim_messages_key = utils.scope_claim_messages(claim_id,
                                                        CLAIM_MESSAGES_SUFFIX)

        msg_keys = self._get_claimed_message_keys(claim_messages_key)

        with self._client.pipeline() as pipe:
            for msg_key in msg_keys:
                pipe.hgetall(msg_key)

            claimed_msgs = pipe.execute()

        # Update the claim id and claim expiration info
        # for all the messages.
        claims_set_key = utils.scope_claims_set(queue, project,
                                                QUEUE_CLAIMS_SUFFIX)

        with self._client.pipeline() as pipe:
            pipe.srem(claims_set_key, claim_id)
            pipe.delete(claim_id)
            pipe.delete(claim_messages_key)

            for msg in claimed_msgs:
                if msg:
                    msg = messages.Message.from_redis(msg)
                    msg.claim_id = None
                    msg.claim_expires = now

                    # TODO(kgriffs): Rather than writing back the
                    # entire message, only set the fields that
                    # have changed.
                    msg.to_redis(pipe)

            self._queue_ctrl._inc_claimed(queue, project,
                                          -1 * len(claimed_msgs),
                                          pipe=pipe)

            pipe.execute()


def _msg_would_expire(message, now):
    return message.expires < now
