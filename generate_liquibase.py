import os
import json
from django.conf import settings

# 1. Initialize an isolated Django sandbox context to bypass settings exceptions
if not settings.configured:
    settings.configure(
        USE_TZ=True,
        INSTALLED_APPS=[
            "apps.email_sender",
            "apps.report_generation",
        ],
    )

import mongoengine
from mongoengine.base.fields import BaseField

# Explicitly list target modules to inspect
MODEL_FILES = [
    "apps.email_sender.models",
    "apps.report_generation.models",
]

BASE_DIR = os.path.join("db", "changelog", "mongodb")
VERSION = "1.0.0"
DDL_DIR = os.path.join(BASE_DIR, VERSION, "ddl")
VERSION_CHANGELOG = os.path.join(BASE_DIR, VERSION, f"changelog-{VERSION}.xml")
MASTER_CHANGELOG = os.path.join(BASE_DIR, "changelog-master.xml")
AUTHOR = "EngineerCharlie"

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
        # Skip internal mongo id field references if not customized
        if field_name == "id" and field_obj.__class__.__name__ == "ObjectIdField":
            continue

        bson_type = get_bson_type(field_obj)
        field_schema = {"bsonType": bson_type}

        # Handle structural recursion for Embedded documents nested inside objects
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
            # Dynamically import via full module lookup string paths
            mod = __import__(module_path, fromlist=["*"])
            # Split out app label token identifier (e.g. 'user' or 'email_sender')
            app_label = module_path.split(".")[1]

            if app_label not in app_groups:
                app_groups[app_label] = []

            # Check module attributes for evaluated MongoEngine Document models
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if isinstance(obj, type) and issubclass(
                    obj, (mongoengine.Document, mongoengine.DynamicDocument)
                ):
                    if obj in (mongoengine.Document, mongoengine.DynamicDocument):
                        continue

                    meta = getattr(obj, "_meta", {})
                    # Skip sub-inheritance types that do not own independent collections
                    if meta.get("abstract", False):
                        continue

                    collection_name = meta.get("collection", obj.__name__.lower())

                    # Capture runtime index criteria evaluations
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
    generated_files = []

    # Flatten out all collections from all apps to generate one file per collection
    all_collections = []
    for app_label, collections in app_data.items():
        all_collections.extend(collections)

    for col in all_collections:
        c_name = col["collection_name"]

        # Name the file explicitly after the collection instead of the app label
        file_name = f"create-{c_name.replace('_', '-')}-collection.xml"

        # Open clean wrapper string profile
        xml_content = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        xml_content += "<databaseChangeLog\n"
        xml_content += '        xmlns="http://www.liquibase.org/xml/ns/dbchangelog"\n'
        xml_content += '        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"\n'
        xml_content += (
            '        xmlns:ext="http://www.liquibase.org/xml/ns/dbchangelog-ext"\n'
        )
        xml_content += '        xsi:schemaLocation="http://www.liquibase.org/xml/ns/dbchangelog https://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-latest.xsd\n'
        xml_content += '        http://www.liquibase.org/xml/ns/dbchangelog-ext https://www.liquibase.org/xml/ns/dbchangelog/dbchangelog-ext.xsd">\n\n'

        json_schema_str = json.dumps(col["schema"], indent=4)
        indented_json = "\n".join(
            [f"                    {line}" for line in json_schema_str.splitlines()]
        )

        # ChangeSet 1: Create Collection with nested Schema Definitions
        xml_content += (
            f'    <changeSet id="create-{c_name}-collection" author="{AUTHOR}">\n'
        )
        xml_content += "        <preConditions>\n"
        xml_content += "            <not>\n"
        xml_content += (
            f'                <ext:collectionExists collectionName="{c_name}"/>\n'
        )
        xml_content += "            </not>\n"
        xml_content += "        </preConditions>\n"
        xml_content += f'        <ext:createCollection collectionName="{c_name}">\n'
        xml_content += "            <options>\n"
        xml_content += '                <option name="validator">\n'
        xml_content += "                    <document><![CDATA[\n"
        xml_content += f"{indented_json}\n"
        xml_content += "                    ]]></document>\n"
        xml_content += "                </option>\n"
        xml_content += (
            '                <option name="validationLevel" value="moderate"/>\n'
        )

        xml_content += (
            '                <option name="validationAction" value="warn"/>\n'
        )
        xml_content += "            </options>\n"
        xml_content += "        </ext:createCollection>\n"
        xml_content += "    </changeSet>\n\n"

        # ChangeSet 2: Unique Fields
        for field in col["unique_fields"]:
            xml_content += f'    <changeSet id="create-{c_name}-index-{field}-unique" author="{AUTHOR}">\n'
            xml_content += f'        <ext:createIndex collectionName="{c_name}">\n'
            xml_content += f'            <ext:keys>{{ "{field}": 1 }}</ext:keys>\n'
            xml_content += f'            <ext:options>{{ "name": "idx_{c_name}_{field}_unique", "unique": true }}</ext:options>\n'
            xml_content += "        </ext:createIndex>\n"
            xml_content += "    </changeSet>\n\n"

        # ChangeSet 3: Meta Class dictionary indexes
        for idx_def in col["indexes"]:
            if isinstance(idx_def, str):
                xml_content += f'    <changeSet id="create-{c_name}-index-{idx_def}" author="{AUTHOR}">\n'
                xml_content += f'        <ext:createIndex collectionName="{c_name}">\n'
                xml_content += (
                    f'            <ext:keys>{{ "{idx_def}": 1 }}</ext:keys>\n'
                )
                xml_content += f'            <ext:options>{{ "name": "idx_{c_name}_{idx_def}" }}</ext:options>\n'
                xml_content += "        </ext:createIndex>\n"
                xml_content += "    </changeSet>\n\n"
            elif isinstance(idx_def, dict) and "fields" in idx_def:
                fields_list = idx_def["fields"]
                idx_name = f"idx_{c_name}_" + "_".join(fields_list)
                keys_expr = ", ".join([f'"{f}": 1' for f in fields_list])
                is_unique = ', "unique": true' if idx_def.get("unique") else ""

                xml_content += f'    <changeSet id="create-{c_name}-index-compound" author="{AUTHOR}">\n'
                xml_content += f'        <ext:createIndex collectionName="{c_name}">\n'
                xml_content += f"            <ext:keys>{{ {keys_expr} }}</ext:keys>\n"
                xml_content += f'            <ext:options>{{ "name": "{idx_name}"{is_unique} }}</ext:options>\n'
                xml_content += "        </ext:createIndex>\n"
                xml_content += "    </changeSet>\n\n"

        xml_content += "</databaseChangeLog>\n"

        full_path = os.path.join(DDL_DIR, file_name)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(xml_content)

        print(f"Generated DDL: {full_path}")
        generated_files.append(file_name)

    return sorted(generated_files)


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
    # Target file format expected by Liquibase
    target_include = f'<include file="mongodb/{VERSION}/changelog-{VERSION}.xml" relativeToChangelogFile="true"/>'

    # Check if master exists to preserve its previous entries
    if os.path.exists(MASTER_CHANGELOG):
        with open(MASTER_CHANGELOG, "r", encoding="utf-8") as f:
            content = f.read()

        # If the target version inclusion is already present, nothing to append
        if f"mongodb/{VERSION}/changelog-{VERSION}.xml" in content:
            print(
                f"ℹ️  Master changelog already contains the target version inclusion profile."
            )
            return

        # Locate the closing tag to dynamically insert our reference right before it
        if "</databaseChangeLog>" in content:
            parts = content.rsplit("</databaseChangeLog>", 1)
            # Standardize indentation styling automatically
            xml_content = parts[0] + f"    {target_include}\n\n</databaseChangeLog>\n"
        else:
            # Fallback if XML structure is corrupted/malformed
            xml_content = content + f"\n{target_include}\n"
    else:
        # Fallback initialization structure if running completely fresh
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
        print(
            "\n✅ Run complete! Multi-collection file groupings generated using clean string builders."
        )
    else:
        print("\n❌ Extraction returned empty. Verify your Python path configurations.")
