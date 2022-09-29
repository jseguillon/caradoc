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
        default: ./caradoc/
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
        self.log = logging.getLogger("caradoc.plugins.callback.default")

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)

    def v2_playbook_on_start(self, playbook):
        self.log_folder = self.get_option("log_folder")

        # TODO: create a per-run uid directory
        if not os.path.exists(self.log_folder):
            makedirs_safe(self.log_folder)

        with open(os.path.join(self.log_folder, "env.adoc"), "wb") as fd:
            fd.write(to_bytes(CaradocTemplates.env))

        with open(os.path.join(self.log_folder, "docinfo.html"), "wb") as fd:
            fd.write(to_bytes(CaradocTemplates.docinfo))

        self.log.debug("v2_playbook_on_start")
        # TODO MVP: dump vars as adoc ? 
        # TODO MVP: render global ifdev include for later import
        # return self.playbook
        
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
        return

    def v2_runner_on_start(self, host, task):
        self.log.debug("v2_runner_on_start")
        return

    def v2_runner_on_ok(self, result, **kwargs):
        self.log.debug("v2_runner_on_ok")
        if result._task_fields['action']!="gather_facts":
          self._save_task(result)

    def v2_runner_on_unreachable(self, result, **kwargs):
        self.log.debug("v2_runner_on_unreachable")

    def v2_runner_on_failed(self, result, **kwargs):
        self.log.debug("v2_runner_on_failed")

    def v2_runner_on_skipped(self, result, **kwargs):
        self.log.debug("v2_runner_on_skipped")

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
    # TODO: may need some implementation of v2_runner_on_async_XXX ? 

    def _template(self, loader, template, variables, target_path="."):

        env_rel_path = "." # TODO: implement relative path to env file compute if dealing with sub-directories
        _templar = Templar(loader=loader, variables=variables)

        template = CaradocTemplates.jinja_macros + "\n" + CaradocTemplates.common_adoc + "\n" + template
        return _templar.template(
            template,
            preserve_trailing_newlines=True,
            convert_data=False,
            escape_backslashes=True
        )

    def _save_task(self, result):
        task=self._template(self._playbook.get_loader(), CaradocTemplates.task_details, 
                             { "result": result._result })

        # TODO and important point: because asciidoc is plain text we should try to ensure it is possible to git diff two runs. 
        # This means we should not use some tasks uuids that would break this
        # Proposed algorithm : create an id as <<host>>_underscored_task_name#[index]
        # The index would be the nth time the <<host>>_underscored_task_name was used (starting at 1)
        # for now it's just fixed 
        # FIXME alspo beacause not all tasks have a name...
        path = os.path.join(self.log_folder, "task_" + result._task_fields['name'] + ".adoc")
        now = time.strftime(self.TIME_FORMAT, time.localtime())

        # TODO : create sub path if not exist (make separated function)
        with open(path, "wb") as fd:
            fd.write(to_bytes(task))

    def _save_play(self):
        # TODO: get from a self. remembered current playbook a dump lists, summarize etc..
        # currently just a mockup
        play_name="playname"
        task=self._template(self._playbook.get_loader(), CaradocTemplates.tasks_list, 
                             { "play_name": play_name })

        # TODO: same as _save_task TODO.
        path = os.path.join(self.log_folder, play_name + ".adoc")
        now = time.strftime(self.TIME_FORMAT, time.localtime())

        # TODO : create sub path if not exist
        with open(path, "wb") as fd:
            fd.write(to_bytes(task))


class CaradocTemplates:
    # Applied to any adoc template, ensure fragments can be viewed with proper display

    # this jinja section is include on each _template render
    jinja_macros='''
{# TODOs: 
    * a function that returns correct emoji for the status of a task
    * a function to compute refs&links given a task name
    * a funcion to map host to a color, usefull if we graph something in report
    * ...
 #}
{% macro task_label() -%}
{% endmacro %}
'''
    # injected in every produced adoc
    common_adoc='''
include::{{ env_rel_path | default('.') }}/env.adoc[]
'''

    task_details='''
// TODO: inject status, name etc
.🟠 Host y - 00:00:02 Duration [[host2_task,taskname]] <<task_uid1,🆙>>
// TODO: use for loops to create the table. use a common jinja2 functions ?
// TODO: beware of loops :)
[%collapsible%open]
======
link:raw.txt[view raw]
[cols="20,~a",autowidth]
|=======
| Result |
[,json]
-------
{{ result | default({})|to_nice_json }}
-------
|=======
======
'''

    tasks_list='''
== Playbook {{ play_name }} ! MOCKUP ! 
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

// TODO: just a sample of what could be a render
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

    env='''
:toclevels: 2
:docinfo: shared,private-footer
:source-highlighter: highlight.js
:icons: font
:backend: any
// TODO: set option for kroki localhost or any url
:kroki-server-url: https://kroki.io
ifdef::env-vscode[:relfilesuffix: .adoc]

// TODO: better include a github.env ?
ifdef::env-github[]
:source-highlighter: rouge
:rouge-style: github
:!showtitle:
:icons: font
:tip-caption: :bulb:
:note-caption: :information_source:
:important-caption: :heavy_exclamation_mark:
:caution-caption: :fire:
:warning-caption: :warning:
endif::[]
'''
