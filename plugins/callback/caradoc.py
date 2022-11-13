# Copyright (c) 2022 The Caradoc Callback Record Ansible Asciidoc authors
# GNU General Public License v3.0+ (see COPYING or https://www.gnu.org/licenses/gpl-3.0.txt)

from __future__ import absolute_import, division, print_function

# FIXME: some clean to be done on imports - need tox and lint
import datetime
import getpass
import json
import logging
import os
import sys
import socket
from concurrent.futures import ThreadPoolExecutor

from ansible import __version__ as ansible_version, constants as C
from ansible.parsing.ajson import AnsibleJSONEncoder
from ansible.plugins.callback import CallbackBase
from ansible.vars.clean import module_response_deepcopy, strip_internal_keys
# Ansible CLI options are now in ansible.context in >= 2.8
# https://github.com/ansible/ansible/commit/afdbb0d9d5bebb91f632f0d4a1364de5393ba17a

from ansible.template import Templar, AnsibleEnvironment
from ansible.template.vars import AnsibleJ2Vars
from ansible.playbook.loop_control import LoopControl
from jinja2.utils import concat as j2_concat
from jinja2.exceptions import TemplateSyntaxError, UndefinedError
from ansible.module_utils._text import to_native, to_text, to_bytes

from ansible.errors import (
    AnsibleAssertionError,
    AnsibleError,
    AnsibleFilterError,
    AnsibleLookupError,
    AnsibleOptionsError,
    AnsiblePluginRemovedError,
    AnsibleUndefinedVariable,
)
from ansible.utils.display import Display

from ansible.utils.path import makedirs_safe
from ansible.module_utils.common.text.converters import to_bytes
from ansible.utils.unsafe_proxy import wrap_var
import re
from json import JSONEncoder
import time
import difflib
from ansible.module_utils.common._collections_compat import MutableMapping

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

# Cache for templates bytecode, used by CaradocTemplar
CARADOC_CACHE= {}

class CallbackModule(CallbackBase):
    """
    Saves data from an Ansible as asciidoc
    """

    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "awesome"
    CALLBACK_NAME = "caradoc_default"

    TIME_FORMAT = "%b %d %Y %H:%M:%S"
    _host_result_struct = {"changed": 0, "ok": 0, "failed": 0, "skipped":0, "ignored_failed": 0}
    # FIXME deal with nolog (https://github.com/ansible/ansible/blob/3515b3c5fcf011ba9bb63fe069520c7d528e3c54/lib/ansible/executor/task_result.py#L131)
    def __init__(self):
        super().__init__()
        # tasks related to current play
        self.tasks = dict()

        # host results tracked for all plays
        self.play_results = { "plays": {}, "host_results": {"all": self._host_result_struct.copy() } }

        # detailed latest results
        self.latest_tasks = []

        # Current playbook running
        self.play=None

        # Track filenames of plays and count to avoid duplicated
        self.tasks_names_count= dict()
        self.play_names_count = dict()

        # global task count
        self.task_end_count=0
        self.log = logging.getLogger("caradoc.plugins.callback.default")

    def set_options(self, task_keys=None, var_options=None, direct=None):
        super().set_options(task_keys=task_keys, var_options=var_options, direct=direct)

    def v2_playbook_on_start(self, playbook):
        self.log_folder = self.get_option("log_folder")
        # Ensure base log folder exists
        if not os.path.exists(self.log_folder):
            makedirs_safe(self.log_folder)
        if not os.path.exists(f"{self.log_folder}/.caradoc.env.adoc"):
            # Dump default statics adoc env
            with open(os.path.join(self.log_folder, ".caradoc.env.adoc"), "wb") as fd:
                fd.write(to_bytes(CaradocTemplates.env))

        # Create a per playbook directory
        # FIXME: not good for git diff => prefer a upper directory then an id just like tasks
        # FIXME: need more precesion
        now = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        self.log_folder = os.path.join(self.log_folder, now)

        self.run_date = time.strftime("%Y/%m/%d - %H:%M:%S", time.localtime())
        if not os.path.exists(self.log_folder):
            makedirs_safe(self.log_folder)

        self.log.debug("v2_playbook_on_start")

        self._playbook=playbook
        return

    def v2_playbook_on_play_start(self, play):
        self.log.debug("v2_playbook_on_play_start")
        play_name=re.sub(r"[^0-9a-zA-Z_\-.]", "_", play.name)
        if play.name in self.play_names_count:
            self.play_names_count[play.name] = self.play_names_count[play.name] + 1
            play_name = f"{play_name}-{str(self.play_names_count[play.name])}"
        else:
            self.play_names_count[play.name] = 1

        if  self.play is not None:
            self._save_play()
            # TODO: ok to loose track of tasks but may should refer plays for global stats
            self.tasks=dict()

        self.play_results["plays"][play._uuid] = { "host_results": {"all": self._host_result_struct.copy()}, "name": play.name }
        self.play = {"name": play.name, "filename": play_name, "_uuid": play._uuid, "tasks": [], "attributes": play.hosts}
        return

    def v2_playbook_on_handler_task_start(self, task):
        self.log.debug("v2_playbook_on_handler_task_start")
        # - from ara - TODO: Why doesn't `v2_playbook_on_handler_task_start` have is_conditional ?
        return ""

    def v2_playbook_on_task_start(self, task, is_conditional, handler=False):
        # Keep only 20 latests
        self.latest_tasks = self.latest_tasks[-20:]

        # TODO: for task duration, see example on https://github.com/alikins/ansible/blob/devel/lib/ansible/plugins/callback/profile_tasks.py
        name=self._get_new_task_name(task)
        self.play["tasks"].append(str(task._uuid))
        self.tasks[task._uuid] = {
            "task_name": wrap_var(task._attributes["name"]),
            "base_path": f"base/{self.play['filename']}/{name}",
            "filename": name,
            "start_time": str(time.time()), "results": {}
        }
        self._save_play()
        self._save_run()
        return

    # Check if couple of task name already referenced and managed a counter
    def _get_new_task_name(self, task):
        name=task._attributes["name"]
        # TODO: track resolved action
        action=task._attributes["action"]
        name="no_name" if name == "" else name
        name=name+"-"+action

        name=re.sub(r"[^0-9a-zA-Z_\-.]", "_", name)

        if name in self.tasks_names_count:
            self.tasks_names_count[name] = self.tasks_names_count[name] + 1
            name=f"{name}-{str(self.tasks_names_count[name])}"
        else:
            self.tasks_names_count[name] = 1
        return name

    def v2_runner_on_start(self, host, task):
        # TODO: render task list with init of running for each host + track start time
        self.log.debug("v2_runner_on_start")
        return

    def v2_runner_on_ok(self, result, **kwargs):
        self.log.debug("v2_runner_on_ok")

        if result._result["changed"]:
            self._save_task(result, "changed")
        else:
            self._save_task(result, "ok")

    def v2_runner_on_unreachable(self, result, **kwargs):
        self.log.debug("v2_runner_on_unreachable")
        self._save_task(result, "unreachable")

    def v2_runner_on_failed(self, result, **kwargs):
        self.log.debug("v2_runner_on_failed")

        if kwargs.get('ignore_errors', False):
            self._save_task(result, "ignored_failed")
        else:
            self._save_task(result, "failed")

    def v2_runner_on_skipped(self, result, **kwargs):
        self.log.debug("v2_runner_on_skipped")
        self._save_task(result, "skipped")

    def v2_runner_item_on_ok(self, result):
        self.log.debug("v2_runner_item_on_ok")

    def v2_runner_item_on_failed(self, result):
        self.log.debug("v2_runner_item_on_failed")

    def v2_runner_item_on_skipped(self, result):
        self.log.debug("v2_runner_item_on_skipped")
        pass
        # from Ara: result._task.delegate_to can end up being a variable from this hook, don't save it.
        # https://github.com/ansible/ansible/issues/75339

    def v2_on_file_diff(self, result):
        current_task = self.tasks[result._task._uuid]
        ansi_escape3 = re.compile(r'(\x9B|\x1B\[)[0-?]*[ -/]*[@-~]', flags=re.IGNORECASE)

        if result._host.name not in current_task["results"]:
            current_task["results"][result._host.name]={}

        if result._task.loop and 'results' in result._result:
            for res in result._result['results']:
                if 'diff' in res and res['diff'] and res.get('changed', False):
                    diff = self._get_diff(res['diff'])
                    if diff:
                        diff = ansi_escape3.sub('', diff)
                        current_task["results"][result._host.name]["diff"]=diff
        elif 'diff' in result._result and result._result['diff'] and result._result.get('changed', False):
            diff = self._get_diff(result._result['diff'])
            if diff:
                diff = ansi_escape3.sub('', diff)
                current_task["results"][result._host.name]["diff"]=wrap_var(diff)


    # TODO: track this event ?
    def v2_playbook_on_include(self, included_file):
        self.log.debug("v2_playbook_on_include")
        pass

    def v2_playbook_on_stats(self, stats):
        self.log.debug("v2_playbook_on_stats")
        # FIXME: save tasks_list shoud be multiple saved each time a runner ends and not waiting playook to achieve
        # self._save_tasks_lists()
        self._save_play()
        self._save_run()
    # TODO: may need some implementation of v2_runner_on_async_XXX also (ara does not implement anything)

    # For a task name, will render base template
    # TODO: split args as separate file since its the same for all results
    def _render_task_result_templates(self,result, task_name, status):
        # TODO: a serializer may be better than this json tricky construction
        # Also in final design may not need all of this an rely or links:[] (for host as an example)
        results = strip_internal_keys(module_response_deepcopy(result._result))
        current_task = self.tasks[result._task._uuid]
        internal_result = current_task["results"][result._host.name]
        jsonified = json.dumps(results, cls=AnsibleJSONEncoder, ensure_ascii=False, sort_keys=False)
        attributes = result._task._attributes
        if "loop_control" in attributes and isinstance(attributes["loop_control"], LoopControl):
            attributes["loop_control"] = None

        # Make unsafe with wrap_vars so it will no try to render internal templates like arg {{ item }} in case of loop
        attributes = wrap_var(attributes)

        json_result = { "result":
                        {
                          "_result": wrap_var(results), # FIXME: also attributes may have template => make unsaffe
                          "_task": {"_attributes": attributes },
                          "_host": {"vars": result._host.vars,
                                    "_uuid": result._host._uuid,
                                    "name": result._host.name,
                                    "address": result._host.address,
                                    "implicit": result._host.implicit },
                          "status": status,
                          "play_name": self.play["filename"],
                          "internal_result": wrap_var(internal_result),
                        }, "env_rel_path": "../../../..", "name": current_task["filename"], "task_name": wrap_var(task_name)
        }
        self._template_and_save(current_task["base_path"], result._host.name + ".adoc", CaradocTemplates.task_details,json_result, cache_name="task_details")

    def _template_and_save(self, path, name, template, tpl_vars, cache_name=None):
        result=self._template(self._playbook.get_loader(), template, tpl_vars, cache_name)
        self._save_as_file(path, name, result)

    # FIXME: deal with handlers
    def _save_task(self, result, status="ok"):
        # Get back name assigned to task uuid for consistent file naming
        # FIXME: save result status + time end etc...
        task=self.tasks[result._task._uuid]

        if result._host.name not in self.play_results["plays"][self.play["_uuid"]]["host_results"]:
            self.play_results["plays"][self.play["_uuid"]]["host_results"][result._host.name] = self._host_result_struct.copy()
        self.play_results["plays"][self.play["_uuid"]]["host_results"][result._host.name][status] = self.play_results["plays"][self.play["_uuid"]]["host_results"][result._host.name][status] + 1

        # TODO: also count per groups ?
        self.play_results["plays"][self.play["_uuid"]]["host_results"]["all"][status] = self.play_results["plays"][self.play["_uuid"]]["host_results"]["all"][status] + 1
        self.play_results["host_results"]["all"][status] = self.play_results["host_results"]["all"][status] + 1
        if result._host.name not in task["results"]:
            task["results"][result._host.name]={}
        task["results"][result._host.name]["status"] = status

        self.task_end_count=self.task_end_count+1

        self._render_task_result_templates(result, task["task_name"], status)
        self._save_task_readme(task)

        if status != "skipped":
            task_in_latest = list(filter(lambda test_list: test_list['task_uuid'] == result._task._uuid, self.latest_tasks))

            if len(task_in_latest) == 0:
                new_task_latest = {"task_uuid": result._task._uuid, "task_name": wrap_var(task["task_name"]), "play_name": self.play["name"], "play_filename": self.play["filename"],
                                    "all_results": self._host_result_struct.copy(), "task_filename": task["filename"]}
                self.latest_tasks.append(new_task_latest)
                task_in_latest = [new_task_latest]
            task_in_latest[0]["all_results"][status] = task_in_latest[0]["all_results"][status] + 1

    def _save_task_readme(self, task):
        json_task_lists={"env_rel_path": "../../../..", "task": task, "play_name": self.play["filename"]}

        self._template_and_save(task["base_path"] +"/", "README.adoc", CaradocTemplates.tasks_list, json_task_lists, cache_name="tasks_list")

    def _save_play(self):
        play_name=self.play["filename"]

        # Dont dump play if no task did run
        if self.play_results["plays"][self.play["_uuid"]]["host_results"]["all"] != self._host_result_struct:
            json_play={ "play": self.play, "env_rel_path": "../../..", "tasks": self.tasks, "hosts_results": self.play_results["plays"][self.play["_uuid"]]["host_results"], "all_mode": False }

            path = f"base/{play_name}/"

            self._template_and_save(path, "README.adoc", CaradocTemplates.playbook, json_play, cache_name="playbook")

            self._template_and_save(path, "charts.adoc", CaradocTemplates.playbook_charts, json_play, cache_name="playbook_charts")

            json_play["all_mode"] = True
            self._template_and_save(path, "all.adoc", CaradocTemplates.playbook, json_play, cache_name="playbook")

    def _save_run(self):
        json_run={ "play_results": self.play_results, "tasks": self.tasks, "latest_tasks": self.latest_tasks, "run_date":self.run_date}

        self._template_and_save("./", "README.adoc", CaradocTemplates.run, json_run, cache_name="run")

    def _save_as_file(self,path,name,content):
        path = os.path.join(self.log_folder, path)
        if not os.path.exists(path):
            makedirs_safe(path)

        path = os.path.join(path, name)
        with open(path, "wb") as fd:
            fd.write(to_bytes(content))

    # Render a caradoc template, including jinja common macros plus static include of env if asked
    def _template(self, loader, template, variables, cache_name, no_env=False):
        # add special variable to refer a cache name for CaradocTemplar
        variables["_cache_name"]=cache_name
        _templar = CaradocTemplar(loader=loader, variables=variables)

        if not no_env:
            template = CaradocTemplates.jinja_macros + "\n" + CaradocTemplates.common_adoc + "\n" + template
        else:
            template = CaradocTemplates.jinja_macros + "\n"  + template
        return _templar.template(
            template
        )

display = Display()

# Specific Templar that deals with bytecode cache
class CaradocTemplar(Templar):

    def __init__(self, loader, shared_loader_obj=None, variables=None):
        super().__init__(loader, shared_loader_obj, variables)
        self.template_cache = {}

    # Note: the template method is a simplified implementation of Templar. Only fail_on_undefined is supported
    def do_template(self, data, preserve_trailing_newlines=True, escape_backslashes=True, fail_on_undefined=None, overrides=None, disable_lookups=False):
        try:
            myenv = self.environment
            cache_name = self._available_variables["_cache_name"]

            try:
                if cache_name not in CARADOC_CACHE:
                    t = myenv.from_string(data)
                    CARADOC_CACHE[cache_name] = t
                else:
                    t = CARADOC_CACHE[cache_name]

            except TemplateSyntaxError as e:
                raise AnsibleError("template error while templating string: %s. String: %s" % (to_native(e), to_native(data)))
            except Exception as e:
                if 'recursion' in to_native(e):
                    raise AnsibleError("recursive loop detected in template string: %s" % to_native(data))
                else:
                    return data

            jvars = AnsibleJ2Vars(self, t.globals)

            ctx = t.new_context(jvars, shared=True)
            rf = t.root_render_func(ctx)

            try:
                res = j2_concat(rf)
            except TypeError as te:
                if 'AnsibleUndefined' in to_native(te):
                    errmsg = "Unable to look up a name or access an attribute in template string (%s).\n" % to_native(data)
                    errmsg += "Make sure your variable name does not contain invalid characters like '-': %s" % to_native(te)
                    raise AnsibleUndefinedVariable(errmsg)
                else:
                    display.debug("failing because of a type error, template data is: %s" % to_text(data))
                    raise AnsibleError("Unexpected templating type error occurred on (%s): %s" % (to_native(data), to_native(te)))
            return res
        except (UndefinedError, AnsibleUndefinedVariable) as e:
            if fail_on_undefined:
                raise AnsibleUndefinedVariable(e)
            else:
                display.debug("Ignoring undefined failure: %s" % to_text(e))
                return data

    # for backwards compatibility in case anyone is using old private method directly
        _do_template = do_template

class CaradocTemplates:
    # Applied to any adoc template, ensure fragments can be viewed with proper display

    # this jinja section is include on each _template render
# //TODO: diffs (https://github.com/ansible/ansible/blob/devel/lib/ansible/plugins/callback/__init__.py#L380)
    jinja_macros='''
{%- macro task_status_label(status) -%}
{%- if status == "ok" -%}🟢
{%- elif status == "changed" -%}🟡
{%- elif status == "failed" -%}🔴
{%- elif status == "ignored_failed" -%}🟣
{%- elif status == "skipped" -%}🔵
{%- elif status == "unreachable" -%}💀
{%- elif status == "running" -%}⚡
{%- endif -%}
{%- endmacro %}

{%- macro get_vega_donut(host, hosts_results) -%}
[vegalite,format="svg",subs="attributes"]
....
{
  "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
  "title": { "text": "{{ host }}", "color": "{caradoc_label_color}", "fontSize": 16 },
  "background": null,
  "data": {
    "values": {{ hosts_results[host]  | dict2items(key_name='status') | to_json  }}
  },
  "transform": [
    {
      "filter": "datum.value > 0"
    }],
  "encoding": {
    "theta": {"field": "value", "type": "quantitative", "stack": true},
    "color": {
      "field": "status",
      "type": "nominal",
      "legend": {"labelColor": "{caradoc_label_color}", "titleColor": "{caradoc_label_color}", "titleFontSize": 14, "labelFontSize": 12},
      "scale": {
        "domain": ["changed", "ok", "skipped", "failed", "ignored_failed"],
        "range": ["rgb( 241, 196, 15 )", "rgb( 39, 174, 96 )", "rgb( 41, 128, 185 )", "rgb(231,76, 60)", "rgb(107, 91, 149)"]
      }
    }
  },
  "layer": [
    {"mark": {"type": "arc", "innerRadius":30, "outerRadius": 70}},
    {
      "mark": {"type": "text", "radius": 95, "fontSize":22},
      "encoding": {"text": {"field": "value", "type": "quantitative"}}
    }
  ]
}
....
{%- endmacro %}
'''
    # injected in every produced adoc
    common_adoc='''
ifndef::env-github[]
include::{{ env_rel_path | default('..') }}/.caradoc.env.adoc[]
//env_rel_path
endif::[]
'''

    # Solo task adoc
    # TODO: consider a jinja macro because code seems a bit duplicate
    # TODO: would be cool if by default is open even from include but only if few lines (can we compute number of lines ?)
    # TODO: result => extract usefull values (msg if changed, skip_reason if skipped, error if error(?), others to be collected) and make the rest collapse
    # TODO: host: show vars "ansible_host", "inventory_file" and "inventory_dir" if exists
    # TODO: host: remove or externalize in meta: "_uuid" for git diff possible
    # FIXME: sort tags
    task_details='''
= {{ task_status_label(result.status) }} {{ result._host.name }} - {{ result._task._attributes.name | default("no name") }} - {{ result._task._attributes.action }}

:toc:

== Links

  * task: link:./README.adoc[{{ result._task._attributes.name }}]
  * playbook: link:../README.adoc[{{ result.play_name }}]


{% if result.internal_result.diff | default('') %}
== Diff
=====
[,diff]
-------
{{ result.internal_result.diff | default('') }}
-------
=====
{% endif %}

== Result

=====
[,json]
-------
{{ result._result | default({}) |to_nice_json }}
-------
=====

== Attributes
[cols="10,~",autowidth,stripes=hover]
|====
| action | {{ result._task._attributes["action"] }}
| become | {{ result._task._attributes["become"] }}
| tags | {{ result._task._attributes["tags"] }}
|====

.view all
[%collapsible]
=====
[,json]
-------
{{ result._task._attributes | default({}) |to_nice_json }}
-------
=====

== Host
[cols="10,~",autowidth,stripes=hover]
|====
| name | {{ result._host["name"] }}
| address | {{ result._host["address"] }}
|====

.view all
[%collapsible]
=====
[,json]
-------
{{ result._host | default({}) |to_nice_json }}
-------
=====
'''

    # FIXME: need to be ordered by host name for stable and minimize diff
    tasks_list='''
= TASK: {{ task.task_name }}

== Links

* playbook link:../README.adoc[{{ play_name }}](link:../all.adoc[all tasks])

== Results
{% for task_for_host in task.results | default({}) %}
include::{{ task_for_host }}.adoc[leveloffset=2,lines=1..12;18..-1]
{%endfor%}

'''

    #TODO: use interactive graphif html or if some CARADOC_INTERACTIVE env var is true
    #TODO: find a way to show total
    #TODO: could we throttle to avoid blinking effects
    playbook_charts='''
[.text-center]
{{ get_vega_donut("all", hosts_results) }}

.show hosts
[%collapsible]
====
{% set rows = hosts_results | list | length -1 %}
{% set rows = 5 if rows >=5 else rows %}
[cols="
{%- for i in range(rows) %}
a{% if loop.index != loop.length %},{% endif %}
{%- endfor -%}
"]
|====
{% for host in hosts_results | sort %}
{% if host != "all" %}
|
{{ get_vega_donut(host, hosts_results) }}
{% endif %}
{% endfor %}
{%- for i in range(hosts_results | list | length % rows) %}
|
{% endfor %}
|====
====
'''

    # TODO: create anchors for task on host
    playbook='''
= PLAY: {{ play['name'] | default(play['name']) }}

{% if not all_mode | default(False) %}
include::./charts.adoc[]
{% endif %}


{% if not all_mode | default(False) %}
== Tasks non ok nor skipped
link:./all.adoc[view all]
{% else %}
== All tasks
link:./README.adoc[view playbook summary]
{% endif %}

+++ <style> +++
table tr td:first-child p a {
  text-decoration: none!important;
}
+++ </style> +++

[cols="1,30,~"]
|====
{% for i in play['tasks'] %}
{% set result_sorted=tasks[i]['results'] | dictsort %}
{% for host, result in result_sorted %}
{% if all_mode or ( (result.status | default('running') != 'ok') and (result.status | default('running') != 'skipped') ) %}
| link:++{{ './' + tasks[i].filename + '/' + host + '.adoc' }}++[{{ task_status_label(result.status | default('running')) }}]
| {{ host }}
| link:++{{ './' + tasks[i].filename + '/' + 'README.adoc' }}++[++{{ tasks[i].task_name | default('no_name') | replace("|","\|") }}++]
{% endif %}
{% endfor %}
{% endfor %}
|====

'''

    run='''
= ⚡ | {{ run_date }}

[cols="a,a"]
|====
|
[.text-center]
*Last 20 plays*
|
[.text-center]
*Last 20 tasks (not skipped)*
[%header,cols="70,5,5,5,5"]
[.tasks_longest]
[.emoji_table]
!=====
! Task ! 🟢 ! 🟡 ! 🔴 ! 🟣 !
{% for x in latest_tasks|reverse %}
     link:+++base/{{ x.play_filename }}/{{ x.task_filename }}/README.adoc+++[{{ x.task_name | replace("|","\|") }}] ! {{ x.all_results.ok }} ! {{ x.all_results.changed }} ! {{ x.all_results.failed }} ! {{ x.all_results.ignored_failed }} !
{% endfor %}
!=====
|====

=====
[,json]
-------
{{ play_results | to_nice_json() }}
-------
=====

=====
[,json]
-------
{{ latest_tasks | to_nice_json() }}
-------
=====
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
:kroki-server-url: http://localhost:8000
ifdef::env-vscode[]
:relfilesuffix: .adoc
:source-highlighter: highlight.js
endif::[]

ifeval::["{caradoc-theme}" != "dark"]
:caradoc_label_color: black
endif::[]
ifeval::["{caradoc-theme}" == "dark"]
:caradoc_label_color: white
+++ <style> a, a:hover { color: #8cb4ff } a:hover {text-decoration: none} </style>+++
+++ <style> code { background: transparent !important; color: white !important }  .hljs-keyword,.hljs-link,.hljs-literal,.hljs-name,.hljs-symbol{color:#569cd6}.hljs-addition,.hljs-deletion{display:inline-block;width:100%}.hljs-link{text-decoration:underline}.hljs-built_in,.hljs-type{color:#4ec9b0}.hljs-class,.hljs-number{color:#b8d7a3}.hljs-meta-string,.hljs-string{color:#d69d85}.hljs-regexp,.hljs-template-tag{color:#9a5334}.hljs-formula,.hljs-function,.hljs-params,.hljs-subst,.hljs-title{color:#dcdcdc}.hljs-comment,.hljs-quote{color:#57a64a;font-style:italic}.hljs-doctag{color:#608b4e}.hljs-meta,.hljs-meta-keyword,.hljs-tag{color:#9b9b9b}.hljs-template-variable,.hljs-variable{color:#bd63c5}.hljs-attr,.hljs-attribute,.hljs-builtin-name{color:#9cdcfe}.hljs-section{color:gold}.hljs-emphasis{font-style:italic}.hljs-strong{font-weight:700}.hljs-bullet,.hljs-selector-attr,.hljs-selector-class,.hljs-selector-id,.hljs-selector-pseudo,.hljs-selector-tag{color:#d7ba7d}.hljs-addition{background-color:var(--vscode-diffEditor-insertedTextBackground,rgba(155,185,85,.2));color:#9bb955}.hljs-deletion{background:var(--vscode-diffEditor-removedTextBackground,rgba(255,0,0,.2));color:red} </style> +++
endif::[]
+++ <style> #header, #content, #footer, #footnotes { max-width: none;} </style> +++
'''
