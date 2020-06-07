import requests

user = 'admin'
pwd = 'Admin@7788'


def get_sys_id_from_INC(url):

    HEADERS= {"Content_Type": "application/json", "Accept":"application/json"}

    response = requests.get(url, auth=(user, pwd))

    if response.status_code !=200:
        #print("something")
        return response.status_code
    x = response.json()
    if (x['result']):
        return x['result'][0]['sys_id']
    else:
        return False

def resolve_incident(sys_id,RC_code,RC_desc):

    url = 'https://dev79084.service-now.com/api/now/table/incident/'+sys_id

    headers = {"Content_Type": "application/json", "Accept":"application/json"}

    response = requests.put(url, auth=(user, pwd), headers=headers, data="{\"incident_state\":\"6\",\"close_code\":\""+RC_code+"\",\"short_description\":\""+RC_desc+"\"}")
    if response.status_code !=200:
        print("Successfull")
        return response.status_code

def update_jira(sys_id,jira_num):

    url = 'https://dev79084.service-now.com/api/now/table/incident/'+sys_id

    headers = {"Content_Type": "application/json", "Accept":"application/json"}

    response = requests.put(url, auth=(user, pwd), headers=headers, data="{\"u_url\":\""+jira_num+"\"}")
    if response.status_code !=200:
        print("Successfull")
        return response.status_code


def Incident(inc_number,rc_code,rc_description):
    inc_num = inc_number
    inc_rc_code = rc_code
    inc_rc_desc = rc_description
    sys_id = ''

    url = 'https://dev79084.service-now.com/api/now/table/incident?sysparm_query=number%3D'+inc_num+'&sysparm_display_value=true&sysparm_exclude_reference_link=true&sysparm_suppress_pagination_header=true&sysparm_fields=sys_id&sysparm_limit=1'

    sys_id = get_sys_id_from_INC(url)
    if sys_id:
        status = resolve_incident(sys_id,inc_rc_code,inc_rc_desc)

        if status:
            return "Something went wrong, status code :"+str(status)
        else:
            return "Successfully Resolved !"
    sys_id = ''

    return "No matching INC in queue."


def Jira(inc_number,jira_number):
    inc_num = inc_number
    jira_num = jira_number
    sys_id = ''

    url = 'https://dev79084.service-now.com/api/now/table/incident?sysparm_query=number%3D'+inc_num+'&sysparm_display_value=true&sysparm_exclude_reference_link=true&sysparm_suppress_pagination_header=true&sysparm_fields=sys_id&sysparm_limit=1'

    sys_id = get_sys_id_from_INC(url)
    if sys_id:
        status = update_jira(sys_id,jira_number)

        if status:
            return "Something went wrong, status code :"+str(status)
        else:
            return "Successfully Resolved !"
    sys_id = ''

    return "No matching INC in queue."
