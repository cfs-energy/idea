#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License"). You may not use this file except in compliance
#  with the License. A copy of the License is located at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  or in the 'license' file accompanying this file. This file is distributed on an 'AS IS' BASIS, WITHOUT WARRANTIES
#  OR CONDITIONS OF ANY KIND, express or implied. See the License for the specific language governing permissions
#  and limitations under the License.

# Copyright (c) 2020 Phillip Dupuis
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files
# (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge,
# publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import importlib
import inspect
import json
from typing import Dict, Any, List, Type
from pydantic import BaseModel, create_model
from ideasdk.utils import Utils


class ModelSchema:
    def clean_schema(self, schema: Dict[str, Any]) -> None:
        """
        Clean up the resulting JSON schemas by:

        1) Removing titles from JSON schema properties.
           If we don't do this, each property will have its own interface in the
           resulting typescript file (which is a LOT of unnecessary noise).
        2) Getting rid of the useless "An enumeration." description applied to Enums
           which don't have a docstring.
        """
        for prop in schema.get('properties', {}).values():
            prop.pop('title', None)

        if 'enum' in schema and schema.get('description') == 'An enumeration.':
            del schema['description']

    @staticmethod
    def fix_anomalies(schema: Dict):
        """
        The original json schema gets generated as below, where inner model classes
        that are referred from API request response classes, are generated with
        multiple references in definitions.

        schema:
            properties: {
                Car: {
                    $ref: #/definitions/<uuid>__Car
                }
            }
            definitions: {
                <uuid>__Car: {
                },
                ideadatamodel__Car: {
                },
                ListCars: {
                    listing: {
                        $ref: <uuid>__Car
                    }
                }
            }

        goal is to unify all these references to a single reference:
            #/definitions/Car

        one major caveat to this implementation is the Model class names must be unique.
        this issue can be eliminated as long as all model class names are unique across modules.
        """

        # In Pydantic v2, JSON schema definitions are under $defs, not definitions
        defs_key = '$defs' if '$defs' in schema else 'definitions'
        definitions = Utils.get_value_as_dict(defs_key, schema)

        # If no definitions, nothing to fix
        if definitions is None:
            return

        # Create a mapping of definition keys to simplified model names
        model_name_mapping = {}
        problematic_refs = set()

        # First pass - identify all model definitions and map complex keys to simple names
        for key in list(definitions.keys()):
            # Extract the simple model name from complex keys (ideadatamodel__xyz__abc__ModelName → ModelName)
            simple_name = key.split('__')[-1]
            if '__' in key:
                model_name_mapping[key] = simple_name
                # Copy the definition to a simpler name
                if simple_name not in definitions:
                    definitions[simple_name] = definitions[key]

        # Second pass - fix all references in the schema
        def fix_refs_in_object(obj):
            if not isinstance(obj, dict):
                return

            # Fix direct $ref
            if '$ref' in obj:
                ref = obj['$ref']
                if defs_key in ref:
                    ref_path = ref.split('/')[-1]
                    if ref_path in model_name_mapping:
                        obj['$ref'] = f'#/{defs_key}/{model_name_mapping[ref_path]}'
                    elif '__' in ref_path:
                        # Handle complex references that aren't in our mapping
                        simple_ref = ref_path.split('__')[-1]
                        if simple_ref in definitions:
                            obj['$ref'] = f'#/{defs_key}/{simple_ref}'
                        else:
                            problematic_refs.add(ref_path)

            # Recurse into nested properties
            for prop_name, prop_value in obj.items():
                if isinstance(prop_value, dict):
                    fix_refs_in_object(prop_value)
                elif isinstance(prop_value, list):
                    for item in prop_value:
                        if isinstance(item, dict):
                            fix_refs_in_object(item)

        # Fix references in properties
        properties = Utils.get_value_as_dict('properties', schema)
        if properties:
            fix_refs_in_object(properties)

        # Fix references in all definitions
        for definition in definitions.values():
            fix_refs_in_object(definition)

        # Clean up redundant definitions
        for key in list(definitions.keys()):
            if '__' in key and key in model_name_mapping:
                del definitions[key]

        # Handle any remaining problematic references
        if problematic_refs:
            for ref_path in problematic_refs:
                simple_name = ref_path.split('__')[-1]
                if simple_name not in definitions:
                    # Create a placeholder definition
                    definitions[simple_name] = {
                        'type': 'object',
                        'properties': {},
                        'description': f'Auto-generated placeholder for missing reference: {ref_path}',
                    }

    def generate_json_schema(self, models: List[Type[BaseModel]]) -> str:
        """
        Create a top-level '_Master_' model with references to each of the actual models.
        Generate the schema for this model, which will include the schemas for all the
        nested models. Then clean up the schema.

        Updated for Pydantic v2 compatibility.
        """
        try:
            # Create master model with all models as fields
            master_fields = {m.__name__: (m, ...) for m in models}

            # Create the master model with references to all models
            master_model = create_model('_Master_', **master_fields)

            # Initialize model_config if it doesn't exist
            if not hasattr(master_model, 'model_config'):
                master_model.model_config = {}

            # Configure the model to use 'forbid' for extra fields
            master_model.model_config['extra'] = 'forbid'

            # Add our clean_schema function to handle JSON schema generation
            def json_schema_extra(schema, *_):
                self.clean_schema(schema)

            master_model.model_config['json_schema_extra'] = json_schema_extra

            # Generate the JSON schema
            schema = json.loads(json.dumps(master_model.model_json_schema()))

            # Apply IDEA-specific fixes
            self.fix_anomalies(schema)

            # Clean up schema definitions
            defs_key = '$defs' if '$defs' in schema else 'definitions'
            schema_definitions = Utils.get_value_as_dict(defs_key, schema, {})
            for name, definition in schema_definitions.items():
                self.clean_schema(definition)

            return json.dumps(schema, indent=2)

        except Exception:
            raise


def export_schema():
    modules = (
        'user_input',
        'auth',
        'scheduler',
        'filesystem',
        'virtual_desktop',
        'cluster_settings',
        'projects',
        'analytics',
        'app',
        'email_templates',
        'notifications',
        'my_costs',
    )
    models = {}
    for name in modules:
        for suffix in ('model', 'api'):
            module_name = f'ideadatamodel.{name}.{name}_{suffix}'
            try:
                module = importlib.import_module(module_name)
            except ModuleNotFoundError as error:
                if error.name == module_name:
                    continue
                raise
            for key, model in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(model, BaseModel)
                    and model is not BaseModel
                    and key != 'IdeaOpenAPISpecEntry'
                ):
                    models.setdefault(key, model)
    print(
        ModelSchema().generate_json_schema(
            sorted(models.values(), key=lambda m: m.__name__)
        )
    )


if __name__ == '__main__':
    export_schema()
