#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# thoth-solver
# Copyright(C) 2018 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Dependency requirements solving for Python ecosystem."""

from collections import deque
from contextlib import contextmanager
import logging
import typing
from shlex import quote

from thoth.analyzer import CommandError
from thoth.analyzer import run_command
from thoth.python import Source
from thoth.python.exceptions import NotFound

from .base import get_ecosystem_solver
from .python_solver import PythonDependencyParser
from .python_solver import PythonSolver

_LOGGER = logging.getLogger(__name__)


def _create_entry(entry: dict, source: Source = None) ->dict:
    """Filter and normalize the output of pipdeptree entry."""
    entry['package_name'] = entry['package'].pop('package_name')
    entry['package_version'] = entry['package'].pop('installed_version')

    if source:
        entry['index_url'] = source.url
        entry['sha256'] = []
        for item in source.get_package_hashes(entry['package_name'], entry['package_version']):
            entry['sha256'].append(item['sha256'])

    entry.pop('package')
    for dependency in entry['dependencies']:
        dependency.pop('key', None)
        dependency.pop('installed_version', None)

    return entry


def _get_environment_details(python_bin: str) -> list:
    """Get information about packages in environment where packages get installed."""
    cmd = '{} -m pipdeptree --json'.format(python_bin)
    output = run_command(cmd, is_json=True).stdout
    return [_create_entry(entry) for entry in output]


@contextmanager
def _install_requirement(python_bin: str, package: str, version: str = None,
                         index_url: str = None, clean: bool = True) -> None:
    """Install requirements specified using suggested pip binary."""
    previous_version = _pipdeptree(python_bin, package)

    cmd = '{} -m pip install --force-reinstall --no-cache-dir --no-deps {}'.format(
        python_bin, quote(package))
    if version:
        cmd += '=={}'.format(quote(version))
    if index_url:
        cmd += ' --index-url "{}" '.format(quote(index_url))

    _LOGGER.debug("Installing requirement %r in version %r", package, version)
    run_command(cmd)

    yield

    if not clean:
        return

    _LOGGER.debug(
        "Restoring previous environment setup after installation of %r", package)

    if previous_version:
        cmd = '{} -m pip install --force-reinstall ' \
              '--no-cache-dir --no-deps {}=={}'.format(python_bin,
                                                       quote(package),
                                                       quote(previous_version['package']['installed_version']))
        _LOGGER.debug("Installing previous version %r of package %r",
                      package, previous_version['package']['installed_version'])
        result = run_command(cmd, raise_on_error=False)

        if result.return_code != 0:
            _LOGGER.warning("Failed to restore previous environment for package %r (installed version %r, "
                            "previous version %r), the error is not fatal but can affect future actions",
                            package, version, previous_version['package']['installed_version'])
            return
    else:
        _LOGGER.debug("Removing installed package %r", package)
        cmd = '{} -m pip uninstall --yes {}'.format(python_bin, quote(package))
        result = run_command(cmd, raise_on_error=False)

        if result.return_code != 0:
            _LOGGER.warning("Failed to restore previous environment by removing package %r (installed version %r), "
                            "the error is not fatal but can affect future actions", package, version)
            return


def _pipdeptree(python_bin, package_name: str = None, warn: bool = False) -> typing.Optional[dict]:
    """Get pip dependency tree by executing pipdeptree tool."""
    cmd = '{} -m pipdeptree --json'.format(python_bin)

    _LOGGER.debug("Obtaining pip dependency tree using: %r", cmd)
    output = run_command(cmd, is_json=True).stdout

    if not package_name:
        return output

    for entry in output:
        # In some versions pipdeptree does not work with --packages flag, do the logic on out own.
        # TODO: we should probably do difference of reference this output and original environment
        if entry['package']['key'].lower() == package_name.lower():
            return entry

    # The given package was not found.
    if warn:
        _LOGGER.warning("Package %r was not found in pipdeptree output %r", package_name, output)
    return None


def _get_dependency_specification(dep_spec: typing.List[tuple]) -> str:
    """Get string representation of dependency specification as provided by PythonDependencyParser."""
    return ",".join(dep_range[0] + dep_range[1] for dep_range in dep_spec)


def _filter_package_dependencies(package_info: dict) -> dict:
    dependencies = {}

    for dependency in package_info['dependencies']:
        dependencies[dependency['package_name']
                     ] = dependency['required_version']

    return dependencies


def _resolve_versions(solver: PythonSolver, source: Source, package_name: str, version_spec: str) -> typing.List[str]:
    try:
        resolved_versions = solver.solve([package_name + (version_spec or '')], all_versions=True)
    except NotFound:
        _LOGGER.info("No versions were resovled for %r with version specification %r for package index %r", package_name, version_spec, source.url)
        return []
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception(
            "Failed to resolve versions for %r with version spec %r", package_name, version_spec)
        return []

    assert len(resolved_versions.keys()) == 1, "Resolution of one package version ended with multiple packages."
    return list(resolved_versions.values())[0]


def _do_resolve_index(solver: PythonSolver, all_solvers: typing.List[PythonSolver],
                      requirements: typing.List[str], python_version: int = 3,
                      exclude_packages: set = None, transitive: bool = True) -> dict:
    index_url = solver.release_fetcher.source.url
    source = solver.release_fetcher.source
    python_bin = 'python3' if python_version == 3 else 'python2'
    run_command('virtualenv -p python3 venv')
    python_bin = 'venv/bin/' + python_bin
    run_command('{} -m pip install pipdeptree'.format(python_bin))

    packages_seen = set()
    packages = []
    errors = []
    unresolved = []
    unparsed = []
    exclude_packages = exclude_packages or {}
    queue = deque()

    for requirement in requirements:
        _LOGGER.debug("Parsing requirement %r", requirement)
        try:
            dependency = PythonDependencyParser.parse_python(requirement)
        except Exception as exc:
            unparsed.append({
                'requirement': requirement,
                'details': str(exc)
            })
            continue

        if dependency.name in exclude_packages:
            continue

        version_spec = _get_dependency_specification(dependency.spec)
        resolved_versions = _resolve_versions(solver, source, dependency.name, version_spec)
        if not resolved_versions:
            _LOGGER.warning("No versions were resolved for dependency %r in version %r", dependency.name, version_spec)
            unresolved.append({
                'package_name': dependency.name,
                'version_spec': version_spec,
                'index': index_url
            })
        else:
            for version in resolved_versions:
                entry = (dependency.name, version)
                packages_seen.add(entry)
                queue.append(entry)

    environment_details = _get_environment_details(python_bin)

    while queue:
        package_name, package_version = queue.pop()
        _LOGGER.info("Using index %r to discover package %r in version %r", index_url, package_name, package_version)
        try:
            with _install_requirement(python_bin, package_name, package_version, index_url):
                package_info = _pipdeptree(python_bin, package_name, warn=True)
        except CommandError as exc:
            _LOGGER.debug(
                "There was an error during package %r in version %r discovery from %r: %s",
                package_name, package_version, index_url, exc
            )
            errors.append({
                'package_name': package_name,
                'index': index_url,
                'version': package_version,
                'type': 'command_error',
                'details': exc.to_dict()
            })
            continue

        if package_info is None:
            errors.append({
                'package_name': package_name,
                'index': index_url,
                'version': package_version,
                'type': 'not_site_package',
                'details': {
                    'message': 'Failed to get information about installed package, probably not site package'
                }
            })
            continue

        if package_info['package']['installed_version'] != package_version:
            _LOGGER.warning(
                "Requested to install version %r of package %r, but installed version is %r, error is not fatal",
                package_version, package_name, package_info['package']['installed_version']
            )

        if package_info['package']['package_name'] != package_name:
            _LOGGER.warning(
                "Requested to install package %r, but installed package name is %r, error is not fatal",
                package_name, package_info['package']['package_name']
            )

        entry = _create_entry(package_info, source)
        packages.append(entry)

        for dependency in entry['dependencies']:
            dependency_name, dependency_range = dependency['package_name'], dependency['required_version']
            dependency['resolved_versions'] = []

            for dep_solver in all_solvers:
                _LOGGER.info(
                    "Resolving dependency versions for %r with range %r from %r",
                    dependency_name, dependency_range, dep_solver._release_fetcher.source.url
                )
                resolved_versions = _resolve_versions(dep_solver, source, dependency_name, dependency_range)
                _LOGGER.debug(
                    "Resolved versions for package %r with range specifier %r: %s",
                    dependency_name, dependency_range, resolved_versions
                )
                dependency['resolved_versions'].append({
                    'versions': resolved_versions,
                    'index': dep_solver._release_fetcher.source.url
                })

                if not transitive:
                    continue

                for version in resolved_versions:
                    # Did we check this package already - do not check indexes, we manually insert them.
                    seen_entry = (dependency_name, version)
                    if seen_entry not in packages_seen:
                        packages_seen.add(seen_entry)
                        queue.append((dependency_name, version))

    return {
        'tree': packages,
        'errors': errors,
        'unparsed': unparsed,
        'unresolved': unresolved,
        'environment': environment_details
    }


def resolve(requirements: typing.List[str], index_urls: list = None, python_version: int = 3,
            exclude_packages: set = None, transitive: bool = True) -> dict:
    """Resolve given requirements for the given Python version."""
    assert python_version in (2, 3), "Unknown Python version"

    result = []
    all_solvers = []
    for index_url in index_urls:
        source = Source(index_url)
        all_solvers.append(PythonSolver(fetcher_kwargs={'source': source}))

    for solver in all_solvers:
        result.append(_do_resolve_index(
            solver,
            all_solvers,
            requirements,
            python_version,
            exclude_packages,
            transitive
        ))

    return result
