# Copyright (c) 2022 The Caradoc Callback Record Ansible Asciidoc authors
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

# FIXME: some clean to be done on imports - need tox and lint
import datetime
import getpass
import json
import logging
import os
import socket
from concurrent.futures import ThreadPoolExecutor

from ansible import __version__ as ansible_version, constants as C
from ansible.parsing.ajson import AnsibleJSONEncoder
from ansible.plugins.callback import CallbackBase
from ansible.vars.clean import module_response_deepcopy, strip_internal_keys
# Ansible CLI options are now in ansible.context in >= 2.8
# https://github.com/ansible/ansible/commit/afdbb0d9d5bebb91f632f0d4a1364de5393ba17a

from ansible.template import Templar
from ansible.utils.path import makedirs_safe
from ansible.module_utils.common.text.converters import to_bytes
from ansible.utils.unsafe_proxy import wrap_var
from json import JSONEncoder
import time

DOCUMENTATION = """
callback: caradoc
callback_type: notification
# TODO: pydoc
requirements:
  - none ? ?
short_description: Create asciidoc reports of Ansible execution
description:
  - Create asciidoc reports of Ansible execution
options:
    log_folder:
        default: .caradoc/
        description: The folder where log files will be created.
        env:
            - name: ANSIBLE_LOG_FOLDER
        ini:
            - section: callback_log_plays
              key: log_folder
"""

# Task modules for which Caradoc should save host facts like ARA (?)
ANSIBLE_SETUP_MODULES = frozenset(
    [   
        "setup",
        "ansible.builtin.setup",
        "ansible.legacy.setup",
        "gather_facts",
        "ansible.builtin.gather_facts",
        "ansible.legacy.setup",
    ]
)

class CallbackModule(CallbackBase):
    """
    Saves data from an Ansible as asciidoc
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "awesome"
    CALLBACK_NAME = "caradoc_default"

    TIME_FORMAT = "%b %d %Y %H:%M:%S"

    # FIXME deal with nolog (https://github.com/ansible/ansible/blob/3515b3c5fcf011ba9bb63fe069520c7d528e3c54/lib/ansible/executor/task_result.py#L131) 
    def __init__(self):
        super().__init__()
        self.tasks = []
        self.log = logging.getLogger("caradoc.plugins.callback.default")

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)

    def v2_playbook_on_start(self, playbook):
        self.log_folder = self.get_option("log_folder")

        # Ensure base log folder exists
        if not os.path.exists(self.log_folder):
            makedirs_safe(self.log_folder)

        # Create a per playbook directory
        # FIXME: not good for git diff => prefer a upper directory then an id just like tasks
        now = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        self.log_folder = os.path.join(self.log_folder, now)

        if not os.path.exists(self.log_folder):
            makedirs_safe(self.log_folder)

        # Dump default statics adoc env and docinfo
        with open(os.path.join(self.log_folder, "env.adoc"), "wb") as fd:
            fd.write(to_bytes(CaradocTemplates.env))

        with open(os.path.join(self.log_folder, "docinfo.html"), "wb") as fd:
            fd.write(to_bytes(CaradocTemplates.docinfo))

        self.log.debug("v2_playbook_on_start")
        
        self._playbook=playbook
        return

    def v2_playbook_on_play_start(self, play):
        self.log.debug("v2_playbook_on_play_start")
        return

    def v2_playbook_on_handler_task_start(self, task):
        self.log.debug("v2_playbook_on_handler_task_start")
        # - from ara - TODO: Why doesn't `v2_playbook_on_handler_task_start` have is_conditional ?
        return ""

    def v2_playbook_on_task_start(self, task, is_conditional, handler=False):
        # TODO: for task duration, see example on https://github.com/alikins/ansible/blob/devel/lib/ansible/plugins/callback/profile_tasks.py
        name=self._get_new_task_name(task)
        self.tasks.append({"task_name": name, "task": task, "start_time": time.time(), "uuid": task._uuid})
        return

    # Check if couple of task name already referenced and managed a counter
    def _get_new_task_name(self, task):
        name=task._attributes["name"]
        action=str(task.resolved_action)
        name="no_name" if name == "" else name
        name=name+"-"+action
        
        # TODO: only replacing spaces is probably not enough in some task name case
        name=name.replace(" ", "_")

        # TODO: Better algo shoud be found
        found=False; count=1
        # For each refered task: check if removing id after last found separator is found, if so: count
        for i in self.tasks:
            count = count + 1 if '-'.join(i["task_name"].split("-")[:-1]) == name else count
        # Final name is name + separator + 
        name=name+"-"+str(count)
        return name

    def v2_runner_on_start(self, host, task):
        self.log.debug("v2_runner_on_start")
        return

    def v2_runner_on_ok(self, result, **kwargs):
        self.log.debug("v2_runner_on_ok")
        self._save_task(result)

    def v2_runner_on_unreachable(self, result, **kwargs):
        self.log.debug("v2_runner_on_unreachable")
        self._save_task(result, failed=True)

    def v2_runner_on_failed(self, result, **kwargs):
        self.log.debug("v2_runner_on_failed")
        self._save_task(result, failed=True)

    def v2_runner_on_skipped(self, result, **kwargs):
        self.log.debug("v2_runner_on_skipped")
        self._save_task(result, failed=True)

    def v2_runner_item_on_ok(self, result):
        self.log.debug("v2_runner_item_on_ok")

    def v2_runner_item_on_failed(self, result):
        self.log.debug("v2_runner_item_on_failed")

    def v2_runner_item_on_skipped(self, result):
        self.log.debug("v2_runner_item_on_skipped")
        pass
        # from Ara: result._task.delegate_to can end up being a variable from this hook, don't save it.
        # https://github.com/ansible/ansible/issues/75339

    def v2_playbook_on_include(self, included_file):
        self.log.debug("v2_playbook_on_include")
        pass

    def v2_playbook_on_stats(self, stats):
        self.log.debug("v2_playbook_on_stats")
        # TODO: stats tables and maybe graphics
        self._save_play()

    # TODO: may need some implementation of v2_runner_on_async_XXX also (ara does not implement anything) 

    # Render a caradoc template, including jinja common macros plus static include of env if asked
    def _template(self, loader, template, variables, no_env=False):
        _templar = Templar(loader=loader, variables=variables)

        if not no_env:
            template = CaradocTemplates.jinja_macros + "\n" + CaradocTemplates.common_adoc + "\n" + template
        else: 
            template = CaradocTemplates.jinja_macros + "\n"  + template
        return _templar.template(
            template,
            preserve_trailing_newlines=True,
            convert_data=False,
            escape_backslashes=True
        )

    # Get back task name by uiid then add result host
    def _get_task_name_for_host(self,result):
        for i in self.tasks:
            if i["uuid"] == result._task._uuid:
                name=i["task_name"]
        
        name=name+"-"+result._host.name
        return name

    # For a task name, will render raw and base templates
    # Also create symlinks in timelines directory
    def _render_task_result_templates(self,result,task_name, failed=False):
        # TODO: a serializer may be better than this json tricky construction
        # Also in final design may not need all of this an rely or links:[] (for host as an example)
        results = strip_internal_keys(module_response_deepcopy(result._result))
        jsonified = json.dumps(results, cls=AnsibleJSONEncoder, ensure_ascii=False, sort_keys=False)
        json_result = { "result": 
                        {
                          "_result": results,
                          "_task": {"_attributes": wrap_var(result._task._attributes)}, # Make unsafe so it will no try to render internal templates like arg {{ item }} in case of loop
                          "_host": {"vars": result._host.vars, 
                                    "_uuid": result._host._uuid, 
                                    "name": result._host.name, 
                                    "address": result._host.address, 
                                    "implicit": result._host.implicit },
                          "failed": failed,
                        }, "env_rel_path": "..", "name": task_name
        }

        task=self._template(self._playbook.get_loader(), CaradocTemplates.task_raw, json_result, no_env=True)
        self._save_as_file("raw/", task_name + ".json", task)

        task=self._template(self._playbook.get_loader(), CaradocTemplates.task_details, json_result)
        self._save_as_file("base/", task_name + ".adoc", task)

        # TODO: create per host timeline
        if not os.path.exists(self.log_folder+"/timeline/"):
            makedirs_safe(self.log_folder+"/timeline/")
        os.symlink("../base/" + task_name + ".adoc", self.log_folder+"/timeline/"+ str(len(self.tasks)) + " - " + task_name + ".adoc", )

    def _save_task(self, result, failed=False):

        # Get back name assigned to task uuid for consistent file naming
        task_name = self._get_task_name_for_host(result)
        self._render_task_result_templates(result,task_name, failed)
 
    def _save_as_file(self,path,name,content):
        path = os.path.join(self.log_folder, path) 
        if not os.path.exists(path):
            makedirs_safe(path)

        path = os.path.join(path, name) 
        with open(path, "wb") as fd:
            fd.write(to_bytes(content))

    def _save_play(self):
        # TODO: get from a self. remembered current playbook a dump lists, summarize etc..
        # currently just a mockup
        play_name="playname"
        task=self._template(self._playbook.get_loader(), CaradocTemplates.tasks_list, 
                             { "play_name": play_name })

        # TODO: same as _save_task TODO.
        path = os.path.join(self.log_folder, play_name + ".adoc")

        # TODO : create sub path if not exist
        with open(path, "wb") as fd:
            fd.write(to_bytes(task))

class CaradocTemplates:
    # Applied to any adoc template, ensure fragments can be viewed with proper display

    # this jinja section is include on each _template render
    jinja_macros='''
{%- macro task_status_label(task_changed, task_error) -%}
{%- if not(task_changed) and not(task_error) -%}🟢
{%- elif not(task_error) -%}🟠
{%- elif task_error -%}🔴
{%- endif -%}
{%- endmacro %}
//FIXME: skipped
//FIXME: includes
//TODO: diffs (https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/__init__.py#L380)
'''
    # injected in every produced adoc
    common_adoc='''
ifndef::env-github[]
include::{{ env_rel_path | default('.') }}/env.adoc[]
//env_rel_path
endif::[]
'''

    # Raw but well formated
    task_raw='''
{{ result | default({}) |to_nice_json }}
'''

    # Solo task adoc
    # TODO: consider a jinja macro because code seems a bit duplicate
    task_details='''
= {{ task_status_label(result._result.changed |default(False),result.failed |default(False) ) }} {{ result._host.name }} - {{ result._task._attributes.name | default("no name") }} - {{ result._task._attributes.action }} 
:toc:

link:../raw/{{ name + ".json" | urlencode }}[view raw]

== Result 
[%collapsible%open]
=====
[,json]
-------
{{ result._result | default({}) |to_nice_json }}
-------
=====

== Attributes
.Attribute
[%collapsible%open]
=====
[,json]
-------
{{ result._task._attributes | default({}) |to_nice_json }}
-------
=====

== Host
.Host
[%collapsible%open]
=====
[,json]
-------
{{ result._host | default({}) |to_nice_json }}
-------
=====
'''

    tasks_list='''
// TODO: curently just a mockup
== Playbook {{ play_name }}
[vegalite]
....
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "description": "A pie chart",
  "background": null,
  "data": {
    "values": [
      {"category": "host_1", "value": 6},
      {"category": "host_2", "value": 8},
      {"category": "host_3", "value": 9},
      {"category": "host_4", "value": 12}
    ]
  },
  "encoding": {
    "color": {"field": "category", "type": "nominal"},
    "theta": {"field": "value", "type": "quantitative", "stack": true},
    "order": {"field": "value", "type": "quantitative", "sort": "descending"}
  },
  "layer": [{"mark": {"type": "arc", "outerRadius": 85}}],
  "view": {"stroke": null}
}
....

[cols="1,30a,1,1,~a,1",autowidth,stripes=hover]
|====
| 🟠 | host_1 | 14:46:47 | 00:00:02 | action 
// .Result
// [%collapsible]
// =====
// include::host1_task1.adoc[tag=snippet-a]
// =====
| <<task_uid1,🔍>>

| 🟢 | host_2 | 14:46:47 | 00:00:02 | action | <<task_uid1,🔍>>
| 🟠 [[first_task_in_timeline]] | host_1 | 14:46:47 | 00:00:02 | quite very long task name with debug name | <<task_uid2,🔍>>
| 🟢 | host_2 | 14:46:47 | 00:00:02 | action | <<task_uid2,🔍>>
| 🟢 | host_x | 14:46:47 | 00:00:02 | action | <<task_uid2,🔍>>
| 🟢 | host_x | 14:46:47 | 00:00:02 | action | <<task_uid2,🔍>>
| 🟢 | host_x | 14:46:47 | 00:00:02 | action | <<task_uid2,🔍>>
| 🟢 | host_1 | 14:46:47 | 00:00:02 | action | <<task_uid1,🔍>>
| 🟢 | host_2 | 14:46:47 | 00:00:02 | action | <<task_uid1,🔍>>

|====

'''

    tasks_list_header='''
'''

    # include header + one list as var + ifdev graphics (?)
    tasks_list_page='''
'''

    docinfo='''
//TODO 
'''

    # or only html 
    env_html='''
'''

    # Mainlys tricks for kroki and vscode
    env='''
:toclevels: 2
// TODO: set env var option for kroki localhost or any url
:kroki-server-url: https://kroki.io
ifdef::env-vscode[]
:relfilesuffix: .adoc
:source-highlighter: highlight.js
endif::[]
'''
