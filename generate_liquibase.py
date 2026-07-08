import os
import json
from django.conf import settings

# 1. Initialize an isolated Django sandbox context to bypass settings exceptions
if not settings.configured:
    settings.configure(
        USE_TZ=True,
        INSTALLED_APPS=[
            "apps.email_sender",
            "apps.metric_analysis",
            "apps.odoo_integration",
            "apps.report_generation",
            "apps.user",
        ],
    )

import mongoengine
from mongoengine.base.fields import BaseField

# Explicitly list target modules to inspect
MODEL_FILES = [
    "apps.email_sender.models",
    "apps.metric_analysis.models",
    "apps.odoo_integration.models",
    "apps.report_generation.models",
    "apps.user.models",
]

BASE_DIR = os.path.join("db", "changelog", "mongodb")
VERSION = "1.2.0"
DDL_DIR = os.path.join(BASE_DIR, VERSION, "ddl")
VERSION_CHANGELOG = os.path.join(BASE_DIR, VERSION, f"changelog-{VERSION}.xml")
MASTER_CHANGELOG = os.path.join(BASE_DIR, "changelog-master.xml")
AUTHOR = "ENGINEER CHARLIE"

# Runtime Type Mapping Engine
TYPE_MAPPING = {
    "StringField": "string",
    "IntField": "int",
    "LongField": "int",
    "FloatField": "double",
    "BooleanField": "bool",
    "DateTimeField": "date",
    "DictField": "object",
    "MapField": "object",
    "ListField": "array",
    "EmbeddedDocumentField": "object",
    "EmbeddedDocumentListField": "array",
    "DynamicField": "object",
    "ObjectIdField": "objectId",
}


def get_bson_type(field: BaseField) -> str:
    """Extracts the exact runtime field type name string."""
    field_class_name = field.__class__.__name__
    return TYPE_MAPPING.get(field_class_name, "string")


def serialize_document_schema(model_cls) -> dict:
    """Recursively crawls a Document configuration to build its true $jsonSchema."""
    properties = {}
    required = []

    for field_name, field_obj in model_cls._fields.items():
        if field_name == "id" and field_obj.__class__.__name__ == "ObjectIdField":
            continue

        bson_type = get_bson_type(field_obj)
        field_schema = {"bsonType": bson_type}

        if field_class := getattr(field_obj, "document_type", None):
            if issubclass(field_class, mongoengine.EmbeddedDocument):
                embedded_props = {}
                embedded_req = []
                for sub_name, sub_obj in field_class._fields.items():
                    sub_type = get_bson_type(sub_obj)
                    embedded_props[sub_name] = {"bsonType": sub_type}
                    if getattr(sub_obj, "required", False):
                        embedded_req.append(sub_name)

                if bson_type == "array":
                    field_schema["items"] = {
                        "bsonType": "object",
                        "properties": embedded_props,
                    }
                    if embedded_req:
                        field_schema["items"]["required"] = embedded_req
                else:
                    field_schema["properties"] = embedded_props
                    if embedded_req:
                        field_schema["required"] = embedded_req

        properties[field_name] = field_schema
        if getattr(field_obj, "required", False):
            required.append(field_name)

    schema = {
        "$jsonSchema": {
            "bsonType": "object",
            "properties": properties,
            "additionalProperties": True,
        }
    }
    if required:
        schema["$jsonSchema"]["required"] = required
    return schema


def extract_runtime_models(modules_list):
    """Imports targets and extracts true metadata directly from MongoEngine's registry."""
    app_groups = {}

    for module_path in modules_list:
        try:
            mod = __import__(module_path, fromlist=["*"])
            app_label = module_path.split(".")[1]

            if app_label not in app_groups:
                app_groups[app_label] = []

            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(
                    obj, (mongoengine.Document, mongoengine.DynamicDocument)
                ):
                    if obj in (mongoengine.Document, mongoengine.DynamicDocument):
                        continue

                    # FIX: Only process the model if it belongs to this exact module file.
                    # This stops base classes imported for inheritance/cross-app references from being processed twice.
                    if getattr(obj, "__module__", "") != module_path:
                        continue

                    meta = getattr(obj, "_meta", {})
                    if meta.get("abstract", False):
                        continue

                    collection_name = meta.get("collection", obj.__name__.lower())

                    unique_fields = [
                        f_name
                        for f_name, f_obj in obj._fields.items()
                        if getattr(f_obj, "unique", False)
                    ]
                    meta_indexes = meta.get("indexes", [])

                    app_groups[app_label].append(
                        {
                            "collection_name": collection_name,
                            "unique_fields": unique_fields,
                            "indexes": meta_indexes,
                            "schema": serialize_document_schema(obj),
                        }
                    )
        except Exception as e:
            print(f"❌ Failed to process runtime initialization for {module_path}: {e}")

    return app_groups


def generate_ddl_files(app_data):
    os.makedirs(DDL_DIR, exist_ok=True)
    generated_files = set()

    all_collections = []
    for app_label, collections in app_data.items():
        all_collections.extend(collections)

    for col in all_collections:
        c_name = col["collection_name"]
        file_name = f"create-{c_name.replace('_', '-')}-collection.xml"
        full_path = os.path.join(DDL_DIR, file_name)

        new_changesets = []

        options_dict = {
            "validator": col["schema"],
            "validationLevel": "moderate",
            "validationAction": "warn",
        }
        json_options_str = json.dumps(options_dict, indent=4)
        indented_options = "\n".join(
            [f"            {line}" for line in json_options_str.splitlines()]
        )

        # ChangeSet 1: Create Collection with nested Schema Definitions & Rollback Strategies
        cs1_id = f"{VERSION}-create-{c_name}-collection"
        cs1_content = (
            f'    <changeSet id="{cs1_id}" author="{AUTHOR}">\n'
            # f'        <preConditions onFail="MARK_RAN">\n'
            # f"            <not>\n"
            # f'                <ext:collectionExists collectionName="{c_name}"/>\n'
            # f"            </not>\n"
            # f"        </preConditions>\n"
            f'        <ext:createCollection collectionName="{c_name}">\n'
            f"            <ext:options><![CDATA[\n"
            f"{indented_options}\n"
            f"            ]]></ext:options>\n"
            f"        </ext:createCollection>\n"
            f"        <rollback>\n"
            f"            <ext:runCommand>\n"
            f"                <ext:command>\n"
            f'                    {{ "drop": "{c_name}" }}\n'
            f"                </ext:command>\n"
            f"            </ext:runCommand>\n"
            f"        </rollback>\n"
            f"    </changeSet>\n\n"
        )
        new_changesets.append((cs1_id, cs1_content))

        # ChangeSet 2: Unique Fields
        for field in col["unique_fields"]:
            idx_name = f"idx_{c_name}_{field}_unique"
            cs2_id = f"{VERSION}-create-{c_name}-index-{field}-unique"
            cs2_content = (
                f'    <changeSet id="{cs2_id}" author="{AUTHOR}">\n'
                f'        <ext:createIndex collectionName="{c_name}">\n'
                f'            <ext:keys>{{ "{field}": 1 }}</ext:keys>\n'
                f'            <ext:options>{{ "name": "{idx_name}", "unique": true }}</ext:options>\n'
                f"        </ext:createIndex>\n"
                f"        <rollback>\n"
                f"            <ext:runCommand>\n"
                f"                <ext:command>\n"
                f'                    {{ "dropIndexes": "{c_name}", "index": "{idx_name}" }}\n'
                f"                </ext:command>\n"
                f"            </ext:runCommand>\n"
                f"        </rollback>\n"
                f"    </changeSet>\n\n"
            )
            new_changesets.append((cs2_id, cs2_content))

        # ChangeSet 3: Meta Class dictionary indexes
        for idx_def in col["indexes"]:
            if isinstance(idx_def, str):
                idx_name = f"idx_{c_name}_{idx_def}"
                cs3_id = f"{VERSION}-create-{c_name}-index-{idx_def}"
                cs3_content = (
                    f'    <changeSet id="{cs3_id}" author="{AUTHOR}">\n'
                    f'        <ext:createIndex collectionName="{c_name}">\n'
                    f'            <ext:keys>{{ "{idx_def}": 1 }}</ext:keys>\n'
                    f'            <ext:options>{{ "name": "{idx_name}" }}</ext:options>\n'
                    f"        </ext:createIndex>\n"
                    f"        <rollback>\n"
                    f"            <ext:runCommand>\n"
                    f"                <ext:command>\n"
                    f'                    {{ "dropIndexes": "{c_name}", "index": "{idx_name}" }}\n'
                    f"                </ext:command>\n"
                    f"            </ext:runCommand>\n"
                    f"        </rollback>\n"
                    f"    </changeSet>\n\n"
                )
                new_changesets.append((cs3_id, cs3_content))
            elif isinstance(idx_def, dict) and "fields" in idx_def:
                fields_list = idx_def["fields"]
                idx_name = f"idx_{c_name}_" + "_".join(fields_list)
                keys_expr = ", ".join([f'"{f}": 1' for f in fields_list])
                is_unique = ', "unique": true' if idx_def.get("unique") else ""

                cs3_id = (
                    f"{VERSION}-create-{c_name}-index-compound-{-'-'.join(fields_list)}"
                )
                cs3_content = (
                    f'    <changeSet id="{cs3_id}" author="{AUTHOR}">\n'
                    f'        <ext:createIndex collectionName="{c_name}">\n'
                    f"            <ext:keys>{{ {keys_expr} }}</ext:keys>\n"
                    f'            <ext:options>{{ "name": "{idx_name}"{is_unique} }}</ext:options>\n'
                    f"        </ext:createIndex>\n"
                    f"        <rollback>\n"
                    f"            <ext:runCommand>\n"
                    f"                <ext:command>\n"
                    f'                    {{ "dropIndexes": "{c_name}", "index": "{idx_name}" }}\n'
                    f"                </ext:command>\n"
                    f"            </ext:runCommand>\n"
                    f"        </rollback>\n"
                    f"    </changeSet>\n\n"
                )
                new_changesets.append((cs3_id, cs3_content))

        # --- File Generation / Appending Logic ---
        if os.path.exists(full_path):
            with open(full_path, "r", encoding="utf-8") as f:
                existing_content = f.read()

            payload_to_append = ""
            for cs_id, cs_body in new_changesets:
                if cs_id in existing_content:
                    print(
                        f"ℹ️  ChangeSet '{cs_id}' already exists in {file_name}. Skipping."
                    )
                    continue
                payload_to_append += cs_body

            if payload_to_append:
                if "</databaseChangeLog>" in existing_content:
                    parts = existing_content.rsplit("</databaseChangeLog>", 1)
                    xml_content = (
                        parts[0] + payload_to_append + "</databaseChangeLog>\n"
                    )
                else:
                    xml_content = existing_content + payload_to_append

                with open(full_path, "w", encoding="utf-8") as f:
                    f.write(xml_content)
                print(f"➕ Appended new changeSets to: {full_path}")
        else:
            xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            xml_content += "<databaseChangeLog\n"
            xml_content += (
                '        xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
            )
            xml_content += (
                '        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
            )
            xml_content += (
                '        xmlns:ext="http://www.liquibase.org/xml/ns/dbchangelog-ext"\n'
            )
            xml_content += '        xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog https://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-latest.xsd\n'
            xml_content += '        http://www.liquibase.org/xml/ns/dbchangelog-ext https://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-ext.xsd">\n\n'

            for _, cs_body in new_changesets:
                xml_content += cs_body

            xml_content += "</databaseChangeLog>\n"

            with open(full_path, "w", encoding="utf-8") as f:
                f.write(xml_content)
            print(f"✨ Generated fresh DDL: {full_path}")

        generated_files.add(file_name)

    return sorted(list(generated_files))


def generate_version_changelog(ddl_files):
    xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    xml_content += "<databaseChangeLog\n"
    xml_content += '        xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
    xml_content += '        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
    xml_content += '        xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog https://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-latest.xsd">\n\n'
    for ddl in ddl_files:
        xml_content += (
            f'    <include file="ddl/{ddl}" relativeToChangelogFile="true"/>\n'
        )
    xml_content += "\n</databaseChangeLog>\n"

    os.makedirs(os.path.dirname(VERSION_CHANGELOG), exist_ok=True)
    with open(VERSION_CHANGELOG, "w", encoding="utf-8") as f:
        f.write(xml_content)


def generate_master_changelog():
    target_include = f'<include file="{VERSION}/changelog-{VERSION}.xml" relativeToChangelogFile="true"/>'

    if os.path.exists(MASTER_CHANGELOG):
        with open(MASTER_CHANGELOG, "r", encoding="utf-8") as f:
            content = f.read()

        if f'file="{VERSION}/changelog-{VERSION}.xml"' in content:
            print("ℹ️  Master changelog already contains target version. Skipping.")
            return

        if "</databaseChangeLog>" in content:
            parts = content.rsplit("</databaseChangeLog>", 1)
            xml_content = parts[0] + f"    {target_include}\n\n</databaseChangeLog>\n"
        else:
            xml_content = content + f"\n{target_include}\n"
    else:
        xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        xml_content += "<databaseChangeLog\n"
        xml_content += '        xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        xml_content += '        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        xml_content += '        xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog https://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-latest.xsd">\n\n'
        xml_content += f"    {target_include}\n\n"
        xml_content += "</databaseChangeLog>\n"

    with open(MASTER_CHANGELOG, "w", encoding="utf-8") as f:
        f.write(xml_content)
    print(f"✨ Updated master changelog at: {MASTER_CHANGELOG}")


if __name__ == "__main__":
    print("🚀 Bootstrapping Runtime Model Registry...")
    data = extract_runtime_models(MODEL_FILES)

    print("📂 Writing dynamic structured Liquibase XML targets...")
    ddl_list = generate_ddl_files(data)

    if ddl_list:
        generate_version_changelog(ddl_list)
        generate_master_changelog()
        print("\n✅ Run complete! Multi-collection file groupings updated securely.")
    else:
        print("\n❌ Extraction returned empty. Verify your Python path configurations.")
