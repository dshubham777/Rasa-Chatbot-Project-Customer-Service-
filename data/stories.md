
## happy_path_1
* greet
  - utter_greet
* inform_resolve_incident_query
  - action_reset_all_slots
  - ticket_info_form
  - form{"name": "ticket_info_form"}
  - form{"name":null}
* inform_affirm
  - action_ticket_api
* inform_thanks
  - utter_ask_if_any_help_required
* goodbye
  - utter_goodbye

## happy_path_2
* greet
  - utter_greet
* inform_resolve_incident_query
  - action_reset_all_slots
  - ticket_info_form
  - form{"name": "ticket_info_form"}
  - form{"name":null}
  - action_set_resolution_desc
  - utter_ask_inc_confirmation
* inform_deny
  - utter_ask_what_you_want_to_change_inc
* inform_field_name
  - action_reset_slot
  - ticket_info_form
  - form{"name": "ticket_info_form"}
  - form{"name":null}
  - utter_ask_inc_confirmation
* inform_affirm
  - action_ticket_api
* inform_thanks
  - utter_ask_if_any_help_required
* goodbye
  - utter_goodbye

## happy_path_3
* greet
  - utter_greet
* inform_update_jira_query
  - action_reset_all_slots
  - jira_info_form
  - form{"name": "jira_info_form"}
  - form{"name":null}
* inform_affirm
  - action_jira_api
* inform_thanks
  - utter_ask_if_any_help_required
* goodbye
  - utter_goodbye


## happy_path_4
* greet
  - utter_greet
* inform_update_jira_query
  - action_reset_all_slots
  - jira_info_form
  - form{"name": "jira_info_form"}
  - form{"name":null}
  - utter_ask_jira_confirmation
* inform_deny
  - utter_ask_what_you_want_to_change_jira
* inform_field_name
  - action_reset_slot
  - jira_info_form
  - form{"name": "jira_info_form"}
  - form{"name":null}
  - utter_ask_jira_confirmation
* inform_affirm
  - action_jira_api
* inform_thanks
  - utter_ask_if_any_help_required
* goodbye
  - utter_goodbye

## say goodbye
* goodbye
  - utter_goodbye



