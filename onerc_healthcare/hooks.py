app_name = "onerc_healthcare"
app_title = "Onerc Healthcare"
app_publisher = "Kelvin Njenga"
app_description = "OneRC Healthcare "
app_email = "njengasheba@gmail.com"
app_license = "mit"

# Send non-GET requests for this app's endpoints as native `application/json`
# bodies instead of form-encoded, per-key JSON-stringified values.
use_json_request_body = True

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "onerc_healthcare",
# 		"logo": "/assets/onerc_healthcare/logo.png",
# 		"title": "Onerc Healthcare",
# 		"route": "/onerc_healthcare",
# 		"has_permission": "onerc_healthcare.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/onerc_healthcare/css/onerc_healthcare.css"
# app_include_js = "/assets/onerc_healthcare/js/onerc_healthcare.js"

# include js, css files in header of web template
# web_include_css = "/assets/onerc_healthcare/css/onerc_healthcare.css"
# web_include_js = "/assets/onerc_healthcare/js/onerc_healthcare.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "onerc_healthcare/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "onerc_healthcare/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "onerc_healthcare.utils.jinja_methods",
# 	"filters": "onerc_healthcare.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "onerc_healthcare.install.before_install"
# after_install = "onerc_healthcare.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "onerc_healthcare.uninstall.before_uninstall"
# after_uninstall = "onerc_healthcare.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "onerc_healthcare.utils.before_app_install"
# after_app_install = "onerc_healthcare.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "onerc_healthcare.utils.before_app_uninstall"
# after_app_uninstall = "onerc_healthcare.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "onerc_healthcare.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "onerc_healthcare.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"onerc_healthcare.tasks.all"
# 	],
# 	"daily": [
# 		"onerc_healthcare.tasks.daily"
# 	],
# 	"hourly": [
# 		"onerc_healthcare.tasks.hourly"
# 	],
# 	"weekly": [
# 		"onerc_healthcare.tasks.weekly"
# 	],
# 	"monthly": [
# 		"onerc_healthcare.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "onerc_healthcare.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "onerc_healthcare.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "onerc_healthcare.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "onerc_healthcare.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["onerc_healthcare.utils.before_request"]
# after_request = ["onerc_healthcare.utils.after_request"]

# Job Events
# ----------
# before_job = ["onerc_healthcare.utils.before_job"]
# after_job = ["onerc_healthcare.utils.after_job"]

# after_file_upload = ["onerc_healthcare.utils.after_file_upload"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"onerc_healthcare.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
export_python_type_annotations = True

# Require all whitelisted methods to have type annotations
require_type_annotated_api_methods = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []

