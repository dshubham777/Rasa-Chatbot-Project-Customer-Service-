# This files contains your custom actions which can be used to run
# custom Python code.
#
# See this guide on how to implement these action:
# https://rasa.com/docs/rasa/core/actions/#custom-actions/


# This is a simple example for a custom action which utters "Hello World!"

from typing import Any, Text, Dict, List, Union

from rasa_sdk import Action, Tracker
from rasa_sdk.executor import CollectingDispatcher
from rasa_sdk.forms import FormAction
from rasa_sdk.events import AllSlotsReset, SlotSet
from close_incident import Incident, Jira
# import logging
#
# logger = logging.getLogger(__name__)
# logging.basicConfig(level='DEBUG')


#Custom Action for resoluton-desc slot mapping
class ActionSetResolutionDesc(Action):

    def name(self):
        return "action_set_resolution_desc"

    def run(self, dispatcher, tracker, domain):
        message = tracker.latest_message['text']
        return [SlotSet('resolution_desc', message)]


#Custom action for reseting all slot
class ActionResetAllSlots(Action):

    def name(self):
        return "action_reset_all_slots"

    def run(self, dispatcher, tracker, domain):
        return [AllSlotsReset()]


#Custon Action to reset particular slot
class ActionResetSlot(Action):

    def name(self):
        return "action_reset_slot"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:
        slot_name = tracker.get_slot('field_name')

        return [SlotSet(slot_name, None)]


# API call to resolve INC
class ActionTicketApi(Action):

    def name(self) -> Text:
        return "action_ticket_api"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        incident_number = tracker.get_slot('incident_number')
        resolution_code = tracker.get_slot('resolution_code')
        resolution_desc = tracker.get_slot('resolution_desc')
        output = Incident(incident_number, resolution_code, resolution_desc)

        dispatcher.utter_template("utter_action_successful", tracker, output=output)

        return []


# API call to update Jira
class ActionJiraApi(Action):

    def name(self) -> Text:
        return "action_jira_api"

    def run(self, dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any]) -> List[Dict[Text, Any]]:

        incident_number = tracker.get_slot('incident_number')
        jira_number = tracker.get_slot('jira_number')
        output = Jira(incident_number, jira_number)
        dispatcher.utter_template("utter_action_successful", tracker, output=output)

        return []


#Form Action for Incident details
class ActionIncFormInfo(FormAction):
    def name(self) -> Text:
        """Unique Identifier of the form"""
        return "ticket_info_form"

    @staticmethod
    def required_slots(tracker: Tracker) -> List[Text]:
        """A list of required slots that the form has to fill"""
        return ["incident_number", "resolution_code", "resolution_desc"]

    def slot_mappings(self) -> Dict[Text, Union[Dict, List[Dict]]]:
        # resolution_desc = tracker.get_slot('resolution_desc')
        # logger.debug(resolution_desc)

        return {
            "incident_number": self.from_entity(entity="incident_number", intent="inform_incident_number"),
            "resolution_code": self.from_entity(entity="resolution_code", intent="inform_resolution_code"),
            "resolution_desc": self.from_text()
        }


    def submit(
            self,
            dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any],
            ) -> List[Dict]:


        return []



#Form Action for Jira details
class ActionJiraFormInfo(FormAction):
    def name(self) -> Text:
        """Unique Identifier of the form"""
        return "jira_info_form"

    @staticmethod
    def required_slots(tracker: Tracker) -> List[Text]:
        """A list of required slots that the form has to fill"""
        return ["incident_number", "jira_number"]

    def submit(
            self,
            dispatcher: CollectingDispatcher,
            tracker: Tracker,
            domain: Dict[Text, Any],
            ) -> List[Dict]:

        return []

    def slot_mappings(self) -> Dict[Text, Union[Dict, List[Dict]]]:
        return {
            "incident_number": self.from_entity(entity="incident_number", intent="inform_incident_number"),
            "jira_number": self.from_entity(entity="jira_number", intent="inform_jira_number")
        }
