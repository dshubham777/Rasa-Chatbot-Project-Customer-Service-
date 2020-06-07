## intent:greet
- hey
- hello
- hi
- good morning
- good evening
- hey there

## intent:goodbye
- bye
- bbye
- goodbye
- see you around
- see you later

## intent:inform_affirm
- yes
- yes I want to update
- please confirm
- yes update

## intent:inform_deny
- No
- No please go ahead
- Nope
- Not now
- no thanks
- no

## intent:inform_thanks
- Thanks
- Thank you
- Thanks for the help

## intent:inform_resolve_incident_query
- yes, I want to resolve a ticket
- resolve a ticket
- i want to resolve a ticket [INC548465](incident_number)
- please resolve my ticket [INC548465](incident_number)

## intent:inform_update_jira_query
- I want to update a jira to INC
- update a jira to incident
- i want to update a jira [REPOIT-1452](incident_number) to INC
- please update my jira [REPOIT-4412](incident_number) to INC

## intent:inform_create_change_request_query
- Create RFC
- Create Change Request
- Please create change request
- please create rfc

## intent:inform_field_name
- [incident_number](field_name)
- [resolution_code](field_name)
- [resolution_desc](field_name)
- [jira_number](field_name)

## intent:inform_incident_number
- [INC5645400](incident_number)
- [INC1452650](incident_number)
- [INC3426781](incident_number)
- [INC6454521](incident_number)
- [INC0458601](incident_number)
- Ticket number is [INC454522](incident_number)
- [INC12365](incident_number)

## intent:inform_resolution_code
- [RC1](resolution_code)
- [RC2](resolution_code)
- [RC3](resolution_code)
- [RC4](resolution_code)
- [RC5](resolution_code)
- [RC6](resolution_code)
- [RC1](resolution_code)

## intent:inform_jira_number
- [REPOIT-1456](jira_number)
- [ALGO-52148](jira_number)
- [ALGO-142](jira_number)


## regex:jira_number
- [A-Za-z]{1,10}-[0-9]{1,10}

## regex:incident_number
- INC[0-9]{1,10}
