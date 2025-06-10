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

import tasks.idea as idea

from ideasdk import notice
from ideasdk.utils import Utils

import importlib
import inspect
import json
import os
import shutil
import sys
from importlib.util import spec_from_file_location, module_from_spec
from tempfile import mkdtemp
from types import ModuleType
from typing import Type, Dict, Any, List, Set, Tuple
from uuid import uuid4
from pydantic import BaseModel, create_model
from pydantic._internal._model_construction import ModelMetaclass
from enum import Enum
import re


class TypingsGenerator:
    """
    Convert IDEA Python models to TypeScript
    This is a modified implementation of @phillipdupuis's pydantic-to-typescript to satisfy IDEA requirements.
    Updated for Pydantic v2 compatibility.

    Source: https://github.com/phillipdupuis/pydantic-to-typescript
    """

    def __init__(self):
        pass

    def import_module(self, path: str) -> ModuleType:
        """
        Helper which allows modules to be specified by either dotted path notation or by filepath.

        If we import by filepath, we must also assign a name to it and add it to sys.modules BEFORE
        calling 'spec.loader.exec_module' because there is code in pydantic which requires that the
        definition exist in sys.modules under that name.
        """
        try:
            if os.path.exists(path):
                name = uuid4().hex
                spec = spec_from_file_location(
                    name, path, submodule_search_locations=[]
                )
                module = module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
                return module
            else:
                return importlib.import_module(path)
        except BaseException as e:
            idea.console.error(
                'The --module argument must be a module path separated by dots or a valid filepath'
            )
            raise e

    def is_submodule(self, obj, module_name: str) -> bool:
        """
        Return true if an object is a submodule
        """
        return inspect.ismodule(obj) and getattr(obj, '__name__', '').startswith(
            f'{module_name}.'
        )

    def is_concrete_pydantic_model(self, obj) -> bool:
        """
        Return true if an object is a concrete subclass of pydantic's BaseModel.
        """
        if not inspect.isclass(obj):
            return False
        elif obj is BaseModel:
            return False
        elif issubclass(obj, Enum):
            return False
        else:
            return issubclass(obj, BaseModel) and isinstance(obj, ModelMetaclass)

    def extract_pydantic_models(
        self, module: ModuleType
    ) -> Set[Tuple[str, Type[BaseModel]]]:
        """
        Given a module, return a list of the pydantic models contained within it.
        """
        models = set()
        module_name = module.__name__

        for name, model in inspect.getmembers(module, self.is_concrete_pydantic_model):
            models.add((name, model))

        for name, submodule in inspect.getmembers(
            module, lambda obj: self.is_submodule(obj, module_name)
        ):
            sub_models = self.extract_pydantic_models(submodule)
            for sub_model in sub_models:
                models.add((sub_model[0], sub_model[1]))

        return models

    def remove_master_model_from_output(self, output: str) -> None:
        """
        A faux 'master model' with references to all the pydantic models is necessary for generating
        clean typescript definitions without any duplicates, but we don't actually want it in the
        output. This function handles removing it from the generated typescript file.
        """
        with open(output, 'r') as f:
            lines = f.readlines()

        start, end = 0, len(lines)
        for i, line in enumerate(lines):
            if line.rstrip('\r\n') == 'export interface _Master_ {':
                start = i
            elif (start is not None) and line.rstrip('\r\n') == '}':
                end = i
                break

        new_lines = lines[:start] + lines[(end + 1) :]
        with open(output, 'w') as f:
            f.writelines(new_lines)

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
                idea.console.info(f'Processing {name} ...')
                self.clean_schema(definition)

            return json.dumps(schema, indent=2)

        except Exception as e:
            idea.console.error(f'Error generating JSON schema: {str(e)}')
            raise

    def remove_null_union_types(self, output: str) -> None:
        """
        Remove the "| null" type unions from the TypeScript file since the fields
        are already marked as optional with the '?' notation.
        """
        try:
            with open(output, 'r') as f:
                content = f.read()

            # Use regex to clean up the TypeScript file
            import re

            # Step 1: Handle simple primitive types and arrays
            primitives = [
                r'string \| null',
                'string',
                r'number \| null',
                'number',
                r'boolean \| null',
                'boolean',
                r'unknown\[\] \| null',
                'unknown[]',
                r'string\[\] \| null',
                'string[]',
            ]

            modified_content = content

            # Replace primitives
            for i in range(0, len(primitives), 2):
                modified_content = re.sub(
                    primitives[i], primitives[i + 1], modified_content
                )

            # Step 2: Handle more complex array types like SomeType[] | null -> SomeType[]
            modified_content = re.sub(r'(\w+)\[\] \| null', r'\1[]', modified_content)

            # Step 3: Handle interface/object reference types like SomeType | null -> SomeType
            modified_content = re.sub(r'(\w+) \| null', r'\1', modified_content)

            # Step 4: Handle object literals in property declarations
            modified_content = re.sub(r'(\{[^{}]*\}) \| null', r'\1', modified_content)

            # Step 5: Handle nested object definitions
            def remove_nulls(match):
                obj_def = match.group(1)
                return obj_def.replace(' | null', '')

            # This pattern matches object literals spanning multiple lines
            multiline_object_pattern = r'(\{\n(?:[^{}]|(?:\{[^{}]*\}))*\n\s*\}) \| null'
            modified_content = re.sub(
                multiline_object_pattern,
                remove_nulls,
                modified_content,
                flags=re.DOTALL,
            )

            # Write the updated content
            with open(output, 'w') as f:
                f.write(modified_content)

            idea.console.info(f'Removed null union types from {output}')
        except Exception as e:
            idea.console.error(f'Error removing null union types: {str(e)}')

    def ensure_copyright_notice(self, output: str) -> None:
        """
        Ensure the IDEA copyright notice is included in the TypeScript file.
        This will replace any existing header comment at the top of the file.
        """
        try:
            with open(output, 'r') as f:
                content = f.read()

            # Prepare IDEA copyright header
            header_lines = [
                '/* tslint:disable */',
                '/* eslint-disable */',
                '/* This file is generated using IDEA invoke web-portal.typings task. */',
                '/* Do not modify this file manually. */',
                '/**',
            ]
            for line in notice.COPYRIGHT_NOTICE.splitlines():
                stripped_line = line.rstrip()
                if stripped_line:
                    header_lines.append(f' * {stripped_line}')
                else:
                    header_lines.append(' *')
            header_lines.append(' */')

            idea_header = '\n'.join(header_lines)

            # Remove any existing header comments at the beginning of the file
            import re

            # Find where the first type or interface declaration begins
            first_content_match = re.search(
                r'(export\s+(?:type|interface|enum))', content
            )
            if first_content_match:
                first_content_pos = first_content_match.start()
                # Replace everything before the first content with our header
                modified_content = idea_header + '\n\n' + content[first_content_pos:]

                with open(output, 'w') as f:
                    f.write(modified_content)

                idea.console.info(f'Updated copyright header in {output}')
            else:
                # If no type or interface found, just prepend our header
                modified_content = idea_header + '\n\n' + content

                with open(output, 'w') as f:
                    f.write(modified_content)

                idea.console.info(f'Added IDEA copyright header to {output}')

        except Exception as e:
            idea.console.error(f'Error ensuring copyright notice: {str(e)}')

    def remove_index_signatures(self, output: str) -> None:
        """
        Remove the index signatures ([k: string]: unknown;) from interfaces in the TypeScript file.
        This preserves the signatures in nested property objects like maps and dictionaries.
        """
        try:
            # Read the file content
            with open(output, 'r') as f:
                content = f.read()

            # Count the initial number of index signatures
            initial_count = content.count('[k: string]: unknown;')

            if initial_count == 0:
                idea.console.info('No index signatures found in the file.')
                return

            idea.console.info(
                f'Found {initial_count} index signatures in the file. Removing...'
            )

            # First approach: Line-by-line processing for accuracy
            lines = content.splitlines()
            result_lines = []

            in_interface = False
            in_nested_object = False
            nested_brace_count = 0

            for line in lines:
                # Check if we're entering an interface
                if re.match(r'^export interface \w+ \{', line.strip()):
                    in_interface = True
                    result_lines.append(line)
                    continue

                # Check if we're entering a nested object property
                if in_interface and '{' in line and '}' not in line:
                    in_nested_object = True
                    nested_brace_count = line.count('{')
                    result_lines.append(line)
                    continue

                # Update nested brace count
                if in_nested_object:
                    nested_brace_count += line.count('{')
                    nested_brace_count -= line.count('}')

                    # Check if we're exiting the nested object
                    if nested_brace_count <= 0:
                        in_nested_object = False

                # Check if we're exiting an interface
                if (
                    in_interface
                    and not in_nested_object
                    and '}' in line
                    and line.strip() == '}'
                ):
                    in_interface = False
                    result_lines.append(line)
                    continue

                # Remove index signatures at the interface level but not in nested objects
                if (
                    in_interface
                    and not in_nested_object
                    and line.strip() == '[k: string]: unknown;'
                ):
                    # Skip this line - it's an interface-level index signature
                    continue

                # Keep all other lines
                result_lines.append(line)

            # Join the lines back together
            new_content = '\n'.join(result_lines)

            # Count how many were removed
            remaining_count = new_content.count('[k: string]: unknown;')
            removed_count = initial_count - remaining_count

            # Write the modified content back to the file
            with open(output, 'w') as f:
                f.write(new_content)

            idea.console.info(
                f'Successfully removed {removed_count} interface-level index signatures, kept {remaining_count} in nested objects.'
            )

        except Exception as e:
            idea.console.error(f'Error removing index signatures: {str(e)}')

    def format_empty_interfaces(self, output: str) -> None:
        """
        Format empty interfaces to stay on a single line.
        Changes from:
            export interface EmptyInterface {
            }
        To:
            export interface EmptyInterface {}
        """
        try:
            with open(output, 'r') as f:
                content = f.read()

            # Find and replace empty interfaces
            import re

            modified_content = re.sub(
                r'(export interface \w+) \{\n\s*\}', r'\1 {}', content
            )

            with open(output, 'w') as f:
                f.write(modified_content)

            idea.console.info(f'Formatted empty interfaces in {output}')
        except Exception as e:
            idea.console.error(f'Error formatting empty interfaces: {str(e)}')

    def extract_array_types(self, schema: Dict) -> Dict[str, Dict[str, str]]:
        """
        Extract array type information from JSON schema.
        Returns a mapping of interface names to their array property types.

        For example:
        {
            "ListProjectsResult": {"listing": "Project"},
            "ListUsersResult": {"listing": "User"}
        }
        """
        array_types = {}

        # In Pydantic v2, JSON schema definitions are under $defs, not definitions
        defs_key = '$defs' if '$defs' in schema else 'definitions'
        definitions = Utils.get_value_as_dict(defs_key, schema, {})

        # Process each definition in the schema
        for interface_name, definition in definitions.items():
            # Skip non-object types or definitions without properties
            if (
                Utils.get_value_as_string('type', definition) != 'object'
                or 'properties' not in definition
            ):
                continue

            properties = definition.get('properties', {})
            array_props = {}

            # Check each property for array types
            for prop_name, prop_def in properties.items():
                # Handle array types
                if (
                    Utils.get_value_as_string('type', prop_def) == 'array'
                    and 'items' in prop_def
                ):
                    items = prop_def.get('items', {})
                    # If the items has a $ref, extract the type
                    if '$ref' in items:
                        ref = items.get('$ref')
                        # Extract the referenced type name
                        ref_type = ref.split('/')[-1]
                        # Store mapping of property name to element type
                        array_props[prop_name] = ref_type
                    # Handle enum arrays or primitive types
                    elif 'type' in items:
                        item_type = items.get('type')
                        if item_type in ['string', 'number', 'boolean', 'integer']:
                            array_props[prop_name] = item_type

            # Only add interfaces with array properties
            if array_props:
                array_types[interface_name] = array_props

        idea.console.info(
            f'Extracted array type information for {len(array_types)} interfaces'
        )
        return array_types

    def deduplicate_interfaces(self, output: str) -> None:
        """
        Deduplicate interfaces that have the same structure but different names
        due to numbering (e.g., JobMetrics, JobMetrics1, JobMetrics2).

        This function:
        1. Identifies interface groups that have the same base name but with number suffixes
        2. Checks if they have the same structure
        3. Replaces all references to the numbered versions with the base version
        4. Removes the duplicate numbered interfaces
        """
        try:
            with open(output, 'r') as f:
                content = f.read()

            import re

            # Find all interfaces with numbered variants (e.g., JobMetrics2, SomeInterface1)
            numbered_pattern = r'export interface (\w+)(\d+) \{([^}]*)\}'
            numbered_matches = re.findall(numbered_pattern, content, re.DOTALL)

            # Group by base name
            interface_groups = {}
            for base_name, number, body in numbered_matches:
                if base_name not in interface_groups:
                    interface_groups[base_name] = []
                interface_groups[base_name].append((number, body))

            # Process each group of numbered interfaces
            for base_name, variants in interface_groups.items():
                idea.console.info(f'Processing numbered variants of {base_name}')

                # Check if unnumbered version exists
                unnumbered_pattern = f'export interface {base_name} ' + r'\{'
                unnumbered_exists = re.search(unnumbered_pattern, content) is not None

                if unnumbered_exists:
                    idea.console.info(
                        f'Unnumbered {base_name} exists, removing numbered variants'
                    )
                    # Remove all numbered variants and update references
                    for number, _ in variants:
                        # Remove the interface definition
                        numbered_def_pattern = (
                            f'export interface {base_name}{number} ' + r'\{[^}]*\}'
                        )
                        content = re.sub(
                            numbered_def_pattern, '', content, flags=re.DOTALL
                        )

                        # Update references to use the unnumbered version
                        content = re.sub(f'{base_name}{number}\\b', base_name, content)
                else:
                    idea.console.info(
                        f'No unnumbered {base_name} found, creating from lowest numbered variant'
                    )
                    # Sort variants by number
                    variants.sort(key=lambda x: int(x[0]))
                    lowest_number, body = variants[0]

                    # Replace the lowest numbered interface with unnumbered
                    content = re.sub(
                        f'export interface {base_name}{lowest_number} ' + r'\{',
                        f'export interface {base_name} ' + '{',
                        content,
                    )

                    # Update references to the lowest numbered interface
                    content = re.sub(
                        f'{base_name}{lowest_number}\\b', base_name, content
                    )

                    # Remove other numbered variants
                    for number, _ in variants:
                        if number != lowest_number:
                            # Remove interface definition
                            numbered_def_pattern = (
                                f'export interface {base_name}{number} ' + r'\{[^}]*\}'
                            )
                            content = re.sub(
                                numbered_def_pattern, '', content, flags=re.DOTALL
                            )

                            # Update references to use unnumbered version
                            content = re.sub(
                                f'{base_name}{number}\\b', base_name, content
                            )

            # Write updated content back to file
            with open(output, 'w') as f:
                # Clean up any multiple consecutive newlines
                content = re.sub(r'\n{3,}', '\n\n', content)
                f.write(content)

            idea.console.info('Deduplicated numbered interfaces')

        except Exception as e:
            idea.console.error(f'Error deduplicating interfaces: {str(e)}')

    def clean_whitespace(self, output: str) -> None:
        """
        Clean up whitespace in a file:
        1. Remove ALL blank lines throughout the file
        2. Ensure exactly one newline at the end of the file
        """
        try:
            # Read the file line by line
            with open(output, 'r') as f:
                lines = f.readlines()

            # Keep only non-blank lines
            result_lines = [line for line in lines if line.strip() != '']

            # Ensure one newline at the end
            if result_lines:
                # Remove any trailing newline from the last line
                result_lines[-1] = result_lines[-1].rstrip('\n')
                # Add exactly one newline
                result_lines[-1] += '\n'

            # Write back to the file
            with open(output, 'w') as f:
                f.writelines(result_lines)

            idea.console.info(f'Removed all blank lines from {output}')

        except Exception as e:
            idea.console.error(f'Error cleaning whitespace: {str(e)}')

    def fix_unknown_arrays(self, output: str) -> None:
        """
        Convert unknown array types to union types.
        Specifically changes:
        listing?: unknown[]; -> listing?: (SocaBaseModel | unknown)[];
        """
        try:
            # Read the file line by line
            with open(output, 'r') as f:
                lines = f.readlines()

            # Look for the pattern in each line
            result_lines = []
            pattern = r'(\s+listing\??:\s+)unknown(\[\];)'

            for line in lines:
                # Use regex to replace the pattern
                import re

                modified_line = re.sub(pattern, r'\1(SocaBaseModel | unknown)\2', line)
                result_lines.append(modified_line)

            # Write back to the file
            with open(output, 'w') as f:
                f.writelines(result_lines)

            idea.console.info(f'Converted unknown arrays to union types in {output}')

        except Exception as e:
            idea.console.error(f'Error fixing unknown arrays: {str(e)}')

    def generate_typescript_defs(
        self,
        modules: List[str],
        output: str,
        exclude: Set[str] = None,
        json2ts_cmd: str = 'json2ts',
    ) -> None:
        """
        Convert the pydantic models in a python module into typescript interfaces.

        :param modules: python modules containing pydantic model definitions, ex: my_project.api.schemas
        :param output: file that the typescript definitions will be written to
        :param exclude: optional, a Set of names for pydantic models which should be omitted from the typescript output.
        :param json2ts_cmd: optional, the command that will execute json2ts. Use this if it's installed in a strange spot.
        """
        if not shutil.which(json2ts_cmd):
            raise Exception(
                'json2ts must be installed. Instructions can be found here: '
                'https://www.npmjs.com/package/json-schema-to-typescript'
            )

        idea.console.info('Finding pydantic models...')

        existing = set()
        models = set()
        for module in modules:
            extracted = self.extract_pydantic_models(self.import_module(module))
            for name, model in extracted:
                if name in existing:
                    continue
                existing.add(name)
                models.add(model)

        if exclude:
            models = [m for m in models if m.__name__ not in exclude]

        models = sorted(list(models), key=lambda m: m.__name__)
        idea.console.info(
            f'Generating {len(models)} JSON schemas from pydantic models ...'
        )

        schema_json = self.generate_json_schema(models)

        schema_dir = mkdtemp()
        schema_file_path = os.path.join(schema_dir, 'schema.json')

        with open(schema_file_path, 'w') as f:
            f.write(schema_json)

        idea.console.info('Converting JSON schema to typescript definitions...')

        banner_comment_items = [
            '/* tslint:disable */',
            '/* eslint-disable */',
            '/* This file is generated using IDEA invoke web-portal.typings task. */',
            '/* Do not modify this file manually. */',
            '/**',
        ]
        for line in notice.COPYRIGHT_NOTICE.splitlines():
            stripped_line = line.rstrip()
            if stripped_line:
                banner_comment_items.append(f' * {stripped_line}')
            else:
                banner_comment_items.append(' *')
        banner_comment_items.append(' */')

        banner_comment = '\n'.join(banner_comment_items)

        # Because of shell escaping issues, write the banner comment to a temp file
        # and use that for the json2ts command
        banner_file_path = os.path.join(schema_dir, 'banner.txt')
        with open(banner_file_path, 'w') as f:
            f.write(banner_comment)

        # Create a dummy output file in case json2ts fails
        temp_output = os.path.join(schema_dir, 'temp_output.ts')

        # Run json2ts with enhanced options to improve reference handling
        exit_code = os.system(
            f'{json2ts_cmd} -i {schema_file_path} -o {temp_output} --banner-file {banner_file_path} --no-strict-nulls --no-union-null --ignore-errors 2>/dev/null'
        )

        # Check if the output file was created
        if (
            exit_code != 0
            or not os.path.exists(temp_output)
            or os.path.getsize(temp_output) == 0
        ):
            idea.console.warn('json2ts conversion failed')

        # Copy the generated file to the final output location
        shutil.copy(temp_output, output)

        shutil.rmtree(schema_dir)

        # If the master model is in the output, remove it
        self.remove_master_model_from_output(output)

        # Remove null union types
        self.remove_null_union_types(output)

        # Remove index signatures
        self.remove_index_signatures(output)

        # Ensure copyright notice is included
        self.ensure_copyright_notice(output)

        # Format empty interfaces to be on a single line
        self.format_empty_interfaces(output)

        # Deduplicate numbered interfaces like JobMetrics, JobMetrics1, JobMetrics2
        self.deduplicate_interfaces(output)

        # Fix unknown arrays to use union types
        self.fix_unknown_arrays(output)

        # Clean up whitespace in the file
        self.clean_whitespace(output)

        idea.console.info(f'Saved typescript definitions to {output}.')
