# Copyright 2018 New Vector Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest
from typing import Optional

from parameterized import parameterized

from synapse import event_auth
from synapse.api.constants import EventContentFields
from synapse.api.errors import AuthError
from synapse.api.room_versions import EventFormatVersions, RoomVersion, RoomVersions
from synapse.events import EventBase, make_event_from_dict
from synapse.types import JsonDict, get_domain_from_id


class EventAuthTestCase(unittest.TestCase):
    def test_rejected_auth_events(self):
        """
        Events that refer to rejected events in their auth events are rejected
        """
        creator = "@creator:example.com"
        auth_events = [
            _create_event(RoomVersions.V9, creator),
            _join_event(RoomVersions.V9, creator),
        ]

        # creator should be able to send state
        event_auth.check_auth_rules_for_event(
            _random_state_event(RoomVersions.V9, creator),
            auth_events,
        )

        # ... but a rejected join_rules event should cause it to be rejected
        rejected_join_rules = _join_rules_event(
            RoomVersions.V9,
            creator,
            "public",
        )
        rejected_join_rules.rejected_reason = "stinky"
        auth_events.append(rejected_join_rules)

        self.assertRaises(
            AuthError,
            event_auth.check_auth_rules_for_event,
            _random_state_event(RoomVersions.V9, creator),
            auth_events,
        )

        # ... even if there is *also* a good join rules
        auth_events.append(_join_rules_event(RoomVersions.V9, creator, "public"))

        self.assertRaises(
            AuthError,
            event_auth.check_auth_rules_for_event,
            _random_state_event(RoomVersions.V9, creator),
            auth_events,
        )

    def test_random_users_cannot_send_state_before_first_pl(self):
        """
        Check that, before the first PL lands, the creator is the only user
        that can send a state event.
        """
        creator = "@creator:example.com"
        joiner = "@joiner:example.com"
        auth_events = [
            _create_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, joiner),
        ]

        # creator should be able to send state
        event_auth.check_auth_rules_for_event(
            _random_state_event(RoomVersions.V1, creator),
            auth_events,
        )

        # joiner should not be able to send state
        self.assertRaises(
            AuthError,
            event_auth.check_auth_rules_for_event,
            _random_state_event(RoomVersions.V1, joiner),
            auth_events,
        )

    def test_state_default_level(self):
        """
        Check that users above the state_default level can send state and
        those below cannot
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"
        king = "@joiner2:example.com"

        auth_events = [
            _create_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, creator),
            _power_levels_event(
                RoomVersions.V1,
                creator,
                {"state_default": "30", "users": {pleb: "29", king: "30"}},
            ),
            _join_event(RoomVersions.V1, pleb),
            _join_event(RoomVersions.V1, king),
        ]

        # pleb should not be able to send state
        self.assertRaises(
            AuthError,
            event_auth.check_auth_rules_for_event,
            _random_state_event(RoomVersions.V1, pleb),
            auth_events,
        ),

        # king should be able to send state
        event_auth.check_auth_rules_for_event(
            _random_state_event(RoomVersions.V1, king),
            auth_events,
        )

    def test_alias_event(self):
        """Alias events have special behavior up through room version 6."""
        creator = "@creator:example.com"
        other = "@other:example.com"
        auth_events = [
            _create_event(RoomVersions.V1, creator),
            _join_event(RoomVersions.V1, creator),
        ]

        # creator should be able to send aliases
        event_auth.check_auth_rules_for_event(
            _alias_event(RoomVersions.V1, creator),
            auth_events,
        )

        # Reject an event with no state key.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _alias_event(RoomVersions.V1, creator, state_key=""),
                auth_events,
            )

        # If the domain of the sender does not match the state key, reject.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _alias_event(RoomVersions.V1, creator, state_key="test.com"),
                auth_events,
            )

        # Note that the member does *not* need to be in the room.
        event_auth.check_auth_rules_for_event(
            _alias_event(RoomVersions.V1, other),
            auth_events,
        )

    def test_msc2432_alias_event(self):
        """After MSC2432, alias events have no special behavior."""
        creator = "@creator:example.com"
        other = "@other:example.com"
        auth_events = [
            _create_event(RoomVersions.V6, creator),
            _join_event(RoomVersions.V6, creator),
        ]

        # creator should be able to send aliases
        event_auth.check_auth_rules_for_event(
            _alias_event(RoomVersions.V6, creator),
            auth_events,
        )

        # No particular checks are done on the state key.
        event_auth.check_auth_rules_for_event(
            _alias_event(RoomVersions.V6, creator, state_key=""),
            auth_events,
        )
        event_auth.check_auth_rules_for_event(
            _alias_event(RoomVersions.V6, creator, state_key="test.com"),
            auth_events,
        )

        # Per standard auth rules, the member must be in the room.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _alias_event(RoomVersions.V6, other),
                auth_events,
            )

    @parameterized.expand([(RoomVersions.V1, True), (RoomVersions.V6, False)])
    def test_notifications(self, room_version: RoomVersion, allow_modification: bool):
        """
        Notifications power levels get checked due to MSC2209.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = [
            _create_event(room_version, creator),
            _join_event(room_version, creator),
            _power_levels_event(
                room_version, creator, {"state_default": "30", "users": {pleb: "30"}}
            ),
            _join_event(room_version, pleb),
        ]

        pl_event = _power_levels_event(
            room_version, pleb, {"notifications": {"room": 100}}
        )

        # on room V1, pleb should be able to modify the notifications power level.
        if allow_modification:
            event_auth.check_auth_rules_for_event(pl_event, auth_events)

        else:
            # But an MSC2209 room rejects this change.
            with self.assertRaises(AuthError):
                event_auth.check_auth_rules_for_event(pl_event, auth_events)

    def test_join_rules_public(self):
        """
        Test joining a public room.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V6, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V6, creator),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V6, creator, "public"
            ),
        }

        # Check join.
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user cannot be force-joined to a room.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _member_event(RoomVersions.V6, pleb, "join", sender=creator),
                auth_events.values(),
            )

        # Banned should be rejected.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "ban"
        )
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user who left can re-join.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "leave"
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user can send a join if they're in the room.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "join"
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user can accept an invite.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "invite", sender=creator
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

    def test_join_rules_invite(self):
        """
        Test joining an invite only room.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V6, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V6, creator),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V6, creator, "invite"
            ),
        }

        # A join without an invite is rejected.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user cannot be force-joined to a room.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _member_event(RoomVersions.V6, pleb, "join", sender=creator),
                auth_events.values(),
            )

        # Banned should be rejected.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "ban"
        )
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user who left cannot re-join.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "leave"
        )
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

        # A user can send a join if they're in the room.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "join"
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

        # A user can accept an invite.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V6, pleb, "invite", sender=creator
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V6, pleb),
            auth_events.values(),
        )

    def test_join_rules_restricted_old_room(self) -> None:
        """Old room versions should reject joins to restricted rooms"""
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V6, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V6, creator),
            ("m.room.power_levels", ""): _power_levels_event(
                RoomVersions.V6, creator, {"invite": 0}
            ),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V6, creator, "restricted"
            ),
        }

        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(RoomVersions.V6, pleb),
                auth_events.values(),
            )

    def test_join_rules_msc3083_restricted(self) -> None:
        """
        Test joining a restricted room from MSC3083.

        This is similar to the public test, but has some additional checks on
        signatures.

        The checks which care about signatures fake them by simply adding an
        object of the proper form, not generating valid signatures.
        """
        creator = "@creator:example.com"
        pleb = "@joiner:example.com"

        auth_events = {
            ("m.room.create", ""): _create_event(RoomVersions.V8, creator),
            ("m.room.member", creator): _join_event(RoomVersions.V8, creator),
            ("m.room.power_levels", ""): _power_levels_event(
                RoomVersions.V8, creator, {"invite": 0}
            ),
            ("m.room.join_rules", ""): _join_rules_event(
                RoomVersions.V8, creator, "restricted"
            ),
        }

        # A properly formatted join event should work.
        authorised_join_event = _join_event(
            RoomVersions.V8,
            pleb,
            additional_content={
                EventContentFields.AUTHORISING_USER: "@creator:example.com"
            },
        )
        event_auth.check_auth_rules_for_event(
            authorised_join_event,
            auth_events.values(),
        )

        # A join issued by a specific user works (i.e. the power level checks
        # are done properly).
        pl_auth_events = auth_events.copy()
        pl_auth_events[("m.room.power_levels", "")] = _power_levels_event(
            RoomVersions.V8,
            creator,
            {"invite": 100, "users": {"@inviter:foo.test": 150}},
        )
        pl_auth_events[("m.room.member", "@inviter:foo.test")] = _join_event(
            RoomVersions.V8, "@inviter:foo.test"
        )
        event_auth.check_auth_rules_for_event(
            _join_event(
                RoomVersions.V8,
                pleb,
                additional_content={
                    EventContentFields.AUTHORISING_USER: "@inviter:foo.test"
                },
            ),
            pl_auth_events.values(),
        )

        # A join which is missing an authorised server is rejected.
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(RoomVersions.V8, pleb),
                auth_events.values(),
            )

        # An join authorised by a user who is not in the room is rejected.
        pl_auth_events = auth_events.copy()
        pl_auth_events[("m.room.power_levels", "")] = _power_levels_event(
            RoomVersions.V8,
            creator,
            {"invite": 100, "users": {"@other:example.com": 150}},
        )
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _join_event(
                    RoomVersions.V8,
                    pleb,
                    additional_content={
                        EventContentFields.AUTHORISING_USER: "@other:example.com"
                    },
                ),
                auth_events.values(),
            )

        # A user cannot be force-joined to a room. (This uses an event which
        # *would* be valid, but is sent be a different user.)
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                _member_event(
                    RoomVersions.V8,
                    pleb,
                    "join",
                    sender=creator,
                    additional_content={
                        EventContentFields.AUTHORISING_USER: "@inviter:foo.test"
                    },
                ),
                auth_events.values(),
            )

        # Banned should be rejected.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "ban"
        )
        with self.assertRaises(AuthError):
            event_auth.check_auth_rules_for_event(
                authorised_join_event,
                auth_events.values(),
            )

        # A user who left can re-join.
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "leave"
        )
        event_auth.check_auth_rules_for_event(
            authorised_join_event,
            auth_events.values(),
        )

        # A user can send a join if they're in the room. (This doesn't need to
        # be authorised since the user is already joined.)
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "join"
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V8, pleb),
            auth_events.values(),
        )

        # A user can accept an invite. (This doesn't need to be authorised since
        # the user was invited.)
        auth_events[("m.room.member", pleb)] = _member_event(
            RoomVersions.V8, pleb, "invite", sender=creator
        )
        event_auth.check_auth_rules_for_event(
            _join_event(RoomVersions.V8, pleb),
            auth_events.values(),
        )


# helpers for making events

TEST_ROOM_ID = "!test:room"


def _create_event(
    room_version: RoomVersion,
    user_id: str,
) -> EventBase:
    return make_event_from_dict(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.create",
            "state_key": "",
            "sender": user_id,
            "content": {"creator": user_id},
        },
        room_version=room_version,
    )


def _member_event(
    room_version: RoomVersion,
    user_id: str,
    membership: str,
    sender: Optional[str] = None,
    additional_content: Optional[dict] = None,
) -> EventBase:
    return make_event_from_dict(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.member",
            "sender": sender or user_id,
            "state_key": user_id,
            "content": {"membership": membership, **(additional_content or {})},
            "prev_events": [],
        },
        room_version=room_version,
    )


def _join_event(
    room_version: RoomVersion,
    user_id: str,
    additional_content: Optional[dict] = None,
) -> EventBase:
    return _member_event(
        room_version,
        user_id,
        "join",
        additional_content=additional_content,
    )


def _power_levels_event(
    room_version: RoomVersion,
    sender: str,
    content: JsonDict,
) -> EventBase:
    return make_event_from_dict(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.power_levels",
            "sender": sender,
            "state_key": "",
            "content": content,
        },
        room_version=room_version,
    )


def _alias_event(room_version: RoomVersion, sender: str, **kwargs) -> EventBase:
    data = {
        "room_id": TEST_ROOM_ID,
        **_maybe_get_event_id_dict_for_room_version(room_version),
        "type": "m.room.aliases",
        "sender": sender,
        "state_key": get_domain_from_id(sender),
        "content": {"aliases": []},
    }
    data.update(**kwargs)
    return make_event_from_dict(data, room_version=room_version)


def _random_state_event(room_version: RoomVersion, sender: str) -> EventBase:
    return make_event_from_dict(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "test.state",
            "sender": sender,
            "state_key": "",
            "content": {"membership": "join"},
        },
        room_version=room_version,
    )


def _join_rules_event(
    room_version: RoomVersion, sender: str, join_rule: str
) -> EventBase:
    return make_event_from_dict(
        {
            "room_id": TEST_ROOM_ID,
            **_maybe_get_event_id_dict_for_room_version(room_version),
            "type": "m.room.join_rules",
            "sender": sender,
            "state_key": "",
            "content": {
                "join_rule": join_rule,
            },
        },
        room_version=room_version,
    )


event_count = 0


def _maybe_get_event_id_dict_for_room_version(room_version: RoomVersion) -> dict:
    """If this room version needs it, generate an event id"""
    if room_version.event_format != EventFormatVersions.V1:
        return {}

    global event_count
    c = event_count
    event_count += 1
    return {"event_id": "!%i:example.com" % (c,)}
