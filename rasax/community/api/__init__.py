from rasax.community.constants import RESPONSE_NAME_KEY

_git_repository = {
    "type": "object",
    "required": ["repository_url"],
    "properties": {
        "name": {"type": "string"},
        "repository_url": {"type": "string"},
        "ssh_key": {"type": "string"},
        "git_service": {"type": "string"},
        "git_service_access_token": {"type": "string"},
        "target_branch": {"type": "string"},
        "use_generated_ssh_keys": {"type": "boolean"},
        "is_target_branch_protected": {"type": "boolean"},
    },
}

_git_repository_update = _git_repository.copy()
_git_repository_update["required"] = []

json_schema = {
    "login": {
        "type": "object",
        "required": ["username", "password"],
        "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
    },
    "change_password": {
        "type": "object",
        "required": [
            "username",
            "old_password",
            "new_password",
            "new_password_confirm",
        ],
        "properties": {
            "username": {"type": "string"},
            "old_password": {"type": "string"},
            "new_password": {"type": "string"},
            "new_password_confirm": {"type": "string"},
        },
    },
    "log": {"type": "string"},
    "data": {
        "type": "object",
        "required": ["text", "intent"],
        "properties": {
            "id": {"type": ["string", "integer"]},
            "text": {"type": "string"},
            "intent": {"type": "string"},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["start", "end", "value", "entity"],
                    "properties": {
                        "start": {"type": ["string", "integer"]},
                        "end": {"type": ["string", "integer"]},
                        "entity": {"type": ["string", "integer"]},
                        "value": {"type": ["string", "integer"]},
                    },
                },
            },
            "annotation": {
                "type": "object",
                "properties": {"user": {"type": "string"}, "time": {"type": "number"}},
            },
            "hash": {"type": "string"},
            "intent_mapped_to": {"type": "string"},
        },
    },
    "handoff": {
        "type": "object",
        "required": ["url"],
        "properties": {"url": {"type": "string"}},
    },
    "user": {
        "type": "object",
        "required": ["username", "password", "roles"],
        "properties": {
            "username": {"type": "string"},
            "password": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
        },
    },
    "user_list": {"type": "array", "items": {"type": "string"}},
    "user_update": {
        "type": "object",
        "required": ["data"],
        "properties": {"data": {}},
        "additionalProperties": True,
    },
    "nlg/response": {
        "type": "object",
        "anyOf": [
            {"required": ["template", "text"]},  # deprecated
            {"required": ["template", "custom"]},  # deprecated
            {"required": [RESPONSE_NAME_KEY, "text"]},
            {"required": [RESPONSE_NAME_KEY, "custom"]},
        ],
        "properties": {
            "template": {"type": "string"},  # deprecated
            RESPONSE_NAME_KEY: {"type": "string"},
            "text": {"type": "string"},
            "buttons": {"type": "array", "items": {"type": "object"}},
            "elements": {"type": "array", "items": {"type": "object"}},
            "image": {"type": "string"},
            "attachment": {"type": "object"},
            "channel": {"type": "string"},
            "custom": {"type": ["object", "array"]},
            "id": {"type": ["string", "integer"]},
            "quick_replies": {"type": "array", "items": {"type": "object"}},
        },
        "additionalProperties": False,
    },
    "nlg/response_bulk": {
        "type": "array",
        "items": {
            "type": "object",
            "anyOf": [
                {"required": ["template", "text"]},  # deprecated
                {"required": ["template", "custom"]},  # deprecated
                {"required": [RESPONSE_NAME_KEY, "text"]},
                {"required": [RESPONSE_NAME_KEY, "custom"]},
            ],
            "properties": {
                "template": {"type": "string"},  # deprecated
                RESPONSE_NAME_KEY: {"type": "string"},
                "text": {"type": "string"},
                "buttons": {"type": "array", "items": {"type": "object"}},
                "elements": {"type": "array", "items": {"type": "object"}},
                "image": {"type": "string"},
                "attachment": {"type": "object"},
                "channel": {"type": "string"},
                "custom": {"type": ["object", "array"]},
                "id": {"type": ["string", "integer"]},
            },
            "additionalProperties": False,
        },
    },
    "nlg/request": {
        "type": "object",
        "anyOf": [
            {"required": ["template", "tracker", "channel", "arguments"]},  # deprecated
            {"required": [RESPONSE_NAME_KEY, "tracker", "channel", "arguments"]},
        ],
        "properties": {
            "template": {"type": "string"},  # deprecated
            RESPONSE_NAME_KEY: {"type": "string"},
            "arguments": {"type": "object"},
            "tracker": {
                "type": "object",
                "properties": {
                    "sender_id": {"type": "string"},
                    "slots": {"type": "object"},
                    "latest_message": {"type": "object"},
                    "latest_event_time": {"type": "number"},
                    "paused": {"type": "boolean"},
                    "events": {"type": "array"},
                    "latest_input_channel": {"type": ["string", "null"]},
                },
            },
            "channel": {
                "type": "object",
                "required": ["name"],
                "properties": {"name": {"type": ["string", "null"]}},
            },
        },
    },
    "nlg/response_name": {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    },
    "story": {
        "type": "object",
        "required": ["story"],
        "properties": {"story": {"type": "string"}},
    },
    "feature": {
        "type": "object",
        "required": ["name", "enabled"],
        "properties": {"name": {"type": "string"}, "enabled": {"type": "boolean"}},
    },
    "user_goal": {
        "type": "object",
        "required": ["name"],
        "properties": {"name": {"type": "string"}},
    },
    "intent/new": {
        "type": "object",
        "required": ["intent"],
        "properties": {
            "intent": {"type": "string"},
            "mapped_to": {"type": ["string", "null"]},
            "user_goal": {"type": "string"},
        },
    },
    "intent": {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "mapped_to": {"type": ["string", "null"]},
            "user_goal": {"type": "string"},
        },
    },
    "message": {
        "type": "object",
        "required": ["message"],
        "properties": {"message": {"type": "string"}},
    },
    "username": {
        "type": "object",
        "required": ["username"],
        "properties": {"username": {"type": "string"}},
    },
    "signed_jwt": {
        "type": "object",
        "required": ["chat_token"],
        "properties": {"chat_token": {"type": "string"},},
    },
    "update_token": {
        "type": "object",
        "required": ["bot_name"],
        "properties": {
            "bot_name": {"type": "string"},
            "description": {"type": "string"},
        },
    },
    "role_list": {"type": "array", "items": {"type": "string"}},
    "role": {
        "type": "object",
        "required": ["role", "grants"],
        "properties": {
            "role": {"type": "string"},
            "grants": {"type": "object"},
            "description": {"type": ["null", "string"]},
            "is_default": {"type": "boolean"},
        },
    },
    "conversation": {
        "type": "object",
        "properties": {
            "sender_id": {"type": "string"},
            "conversation_to_copy_from": {"type": "string"},
            "until": {"type": "number"},
        },
    },
    "action": {
        "type": "object",
        "required": ["name"],
        "properties": {
            "id": {"type": "integer"},
            "domain_id": {"type": "integer"},
            "is_form": {"type": "boolean"},
            "name": {"type": "string"},
        },
    },
    "regex": {
        "type": "object",
        "required": ["name", "pattern"],
        "properties": {
            "id": {"type": "integer"},
            "name": {"type": "string"},
            "pattern": {"type": "string"},
        },
    },
    "lookup_table": {
        "type": "object",
        "required": ["filename", "content"],
        "properties": {"filename": {"type": "string"}, "content": {"type": "string"}},
    },
    "entity_synonym": {
        "type": "object",
        "required": ["synonym_reference", "mapped_values"],
        "properties": {
            "synonym_reference": {"type": "string"},
            "mapped_values": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            },
        },
    },
    "entity_synonym_name": {
        "type": "object",
        "required": ["synonym_reference"],
        "properties": {"synonym_reference": {"type": "string"}},
    },
    "entity_synonym_value": {
        "type": "object",
        "required": ["value"],
        "properties": {"value": {"type": "string"}},
    },
    "entity_synonym_values": {
        "type": "object",
        "required": ["mapped_values"],
        "properties": {
            "mapped_values": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["value"],
                    "properties": {"value": {"type": "string"}},
                },
            }
        },
    },
    "git_repository": _git_repository,
    "git_repository_update": _git_repository_update,
    "telemetry_event": {
        "type": "object",
        "required": ["event_name"],
        "properties": {
            "event_name": {"type": "string"},
            "properties": {"type": "object", "minProperties": 1},
            "context": {"type": "object", "minProperties": 1},
        },
    },
    "conversation_tags": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "value": {"type": "string", "minLength": 1},
                "color": {"type": "string", "minLength": 6, "maxLength": 6},
            },
        },
    },
}
