"""Functional tests for sysconfig charm."""

import asyncio
import os
import subprocess

import pytest


# Treat all tests as coroutines
pytestmark = pytest.mark.asyncio

juju_repository = os.getenv('CHARM_BUILD_DIR', '.').rstrip('/')

series = ['xenial', 'bionic']

sources = [('local', '{}/builds/sysconfig'.format(juju_repository))]

TIMEOUT = 1000
GRUB_DEFAULT = 'Advanced options for Ubuntu>Ubuntu, with Linux {}'
PRINCIPAL_APP_NAME = 'ubuntu-{}'

# Uncomment for re-using the current model, useful for debugging functional tests
# @pytest.fixture(scope='module')
# async def model():
#     from juju.model import Model
#     model = Model()
#     await model.connect_current()
#     yield model
#     await model.disconnect()


# Custom fixtures
@pytest.fixture(params=series)
def series(request):
    """Return ubuntu version (i.e. xenial) in use in the test."""
    return request.param


@pytest.fixture(params=sources, ids=[s[0] for s in sources])
def source(request):
    """Return source of the charm under test (i.e. local, cs)."""
    return request.param


@pytest.fixture
async def app(model, series, source):
    """Return application of the charm under test."""
    app_name = 'sysconfig-{}-{}'.format(series, source[0])
    return await model._wait_for_new('application', app_name)


# Tests

async def test_sysconfig_deploy(model, series, source, request):
    """Deploys the sysconfig charm as a subordinate of ubuntu."""
    channel = 'stable'
    sysconfig_app_name = 'sysconfig-{}-{}'.format(series, source[0])
    principal_app_name = PRINCIPAL_APP_NAME.format(series)

    ubuntu_app = await model.deploy(
        'cs:ubuntu',
        application_name=principal_app_name,
        series=series,
        channel=channel,
    )

    await model.block_until(
        lambda: ubuntu_app.status == 'active',
        timeout=TIMEOUT
    )

    # Using subprocess b/c libjuju fails with JAAS
    # https://github.com/juju/python-libjuju/issues/221
    cmd = ['juju', 'deploy', source[1], '-m', model.info.name,
           '--series', series, sysconfig_app_name]

    if request.node.get_closest_marker('xfail'):
        # If series is 'xfail' force install to allow testing against versions not in
        # metadata.yaml
        cmd.append('--force')
    subprocess.check_call(cmd)

    # This is pretty horrible, but we can't deploy via libjuju
    while True:
        try:
            sysconfig_app = model.applications[sysconfig_app_name]
            break
        except KeyError:
            await asyncio.sleep(5)

    await sysconfig_app.add_relation('juju-info', '{}:juju-info'.format(principal_app_name))

    await model.block_until(
        lambda: sysconfig_app.status == 'blocked',
        timeout=TIMEOUT
    )


async def test_cannot_run_in_container(app):
    """Test that default config doesn't allow to install in container."""
    assert app.status == 'blocked'


async def test_forced_deploy(app, model):
    """Force to install in container for testing purpose."""
    await app.set_config({'enable-container': 'true'})
    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )
    assert app.status == 'active'


async def test_cpufrequtils_intalled(app, jujutools):
    """Verify cpufrequtils pkg is installed."""
    unit = app.units[0]
    cmd = 'dpkg -l | grep cpufrequtils'
    results = await jujutools.run_command(cmd, unit)
    assert results['Code'] == '0'


async def test_default_config(app, jujutools):
    """Test default configuration for grub, systemd and cpufrequtils."""
    unit = app.units[0]

    grup_path = '/etc/default/grub.d/90-sysconfig.cfg'
    grub_content = await jujutools.file_contents(grup_path, unit)
    assert 'isolcpus' not in grub_content
    assert 'hugepages' not in grub_content
    assert 'hugepagesz' not in grub_content
    assert 'raid' not in grub_content
    assert 'pti=off' in grub_content
    assert 'intel_iommu' not in grub_content
    assert 'GRUB_DEFAULT' not in grub_content

    systemd_path = '/etc/systemd/system.conf'
    systemd_content = await jujutools.file_contents(systemd_path, unit)
    systemd_valid = True
    for line in systemd_content:
        if line.startswith('CPUAffinity='):
            systemd_valid = False
    assert systemd_valid

    cpufreq_path = '/etc/default/cpufrequtils'
    cpufreq_content = await jujutools.file_contents(cpufreq_path, unit)
    assert 'GOVERNOR' not in cpufreq_content


async def test_config_changed(app, model, jujutools):
    """Test configuration changed for grub, systemd, cpufrqutils and kernel."""
    kernel_version = '4.15.0-38-generic'
    linux_pkg = 'linux-image-{}'.format(kernel_version)
    linux_modules_extra_pkg = 'linux-modules-extra-{}'.format(kernel_version)

    await app.set_config(
        {
            'reservation': 'isolcpus',
            'cpu-range': '1,2,3,4',
            'hugepages': '100',
            'hugepagesz': '1G',
            'raid-autodetection': 'noautodetect',
            'enable-pti': 'true',
            'enable-iommu': 'false',
            'kernel-version': kernel_version,
            'grub-config-flags': 'GRUB_TIMEOUT=10',
            'systemd-config-flags': 'DefaultLimitRTTIME=1,DefaultTasksMax=10',
            'governor': 'powersave'
        }
    )
    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )
    assert app.status == 'active'

    unit = app.units[0]

    grup_path = '/etc/default/grub.d/90-sysconfig.cfg'
    grub_content = await jujutools.file_contents(grup_path, unit)
    assert 'isolcpus=1,2,3,4' in grub_content
    assert 'hugepages=100' in grub_content
    assert 'hugepagesz=1G' in grub_content
    assert 'raid=noautodetect' in grub_content
    assert 'pti=off' not in grub_content
    assert 'intel_iommu=on iommu=pt' not in grub_content
    assert 'GRUB_DEFAULT="{}"'.format(GRUB_DEFAULT.format(kernel_version)) in grub_content
    assert 'GRUB_TIMEOUT=10' in grub_content

    # Reconfiguring reservation from isolcpus to affinity
    # isolcpus will be removed from grub and affinity added to systemd

    await app.set_config(
        {
            'reservation': 'affinity'
        }
    )

    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )
    assert app.status == 'active'

    systemd_path = '/etc/systemd/system.conf'
    systemd_content = await jujutools.file_contents(systemd_path, unit)

    assert 'CPUAffinity=1,2,3,4' in systemd_content

    assert 'DefaultLimitRTTIME=1' in systemd_content
    assert 'DefaultTasksMax=10' in systemd_content

    grub_content = await jujutools.file_contents(grup_path, unit)
    assert 'isolcpus' not in grub_content

    cpufreq_path = '/etc/default/cpufrequtils'
    cpufreq_content = await jujutools.file_contents(cpufreq_path, unit)
    assert 'GOVERNOR=powersave' in cpufreq_content

    # test new kernel installed
    for pkg in (linux_pkg, linux_modules_extra_pkg):
        cmd = 'dpkg -l | grep {}'.format(pkg)
        results = await jujutools.run_command(cmd, unit)
        assert results['Code'] == '0'

    # test update-status show that reboot is required
    assert "reboot required." in unit.workload_status_message


async def test_clear_notification(app):
    """Tests that clear-notification action complete."""
    unit = app.units[0]
    action = await unit.run_action('clear-notification')
    action = await action.wait()
    assert action.status == 'completed'


async def test_wrong_reservation(app, model):
    """Tests wrong reservation value is used.

    Expect application is blocked until correct value is set.
    """
    await app.set_config(
        {
            'reservation': 'changeme'
        }
    )
    await model.block_until(
        lambda: app.status == 'blocked',
        timeout=TIMEOUT
    )
    assert app.status == 'blocked'
    unit = app.units[0]
    assert 'configuration parameters not valid.' in unit.workload_status_message

    await app.set_config(
        {
            'reservation': 'off'
        }
    )
    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )


async def test_wrong_raid_autodetection(app, model):
    """Tests wrong raid-autodetection value is used.

    Expect application is blocked until correct value is set.
    """
    await app.set_config(
        {
            'raid-autodetection': 'changeme'
        }
    )
    await model.block_until(
        lambda: app.status == 'blocked',
        timeout=TIMEOUT
    )
    assert app.status == 'blocked'
    unit = app.units[0]
    assert 'configuration parameters not valid.' in unit.workload_status_message

    await app.set_config(
        {
            'raid-autodetection': ''
        }
    )
    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )


async def test_wrong_governor(app, model):
    """Tests wrong governor value is used.

    Expect application is blocked until correct value is set.
    """
    await app.set_config(
        {
            'governor': 'changeme'
        }
    )
    await model.block_until(
        lambda: app.status == 'blocked',
        timeout=TIMEOUT
    )
    assert app.status == 'blocked'
    unit = app.units[0]
    assert 'configuration parameters not valid.' in unit.workload_status_message

    await app.set_config(
        {
            'governor': ''
        }
    )
    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )


async def test_uninstall(app, model, jujutools, series):
    """Tests unistall the unit removing the subordinate relation."""
    # Apply systemd and cpufrequtils configuration to test that is deleted
    # after removing the relation with ubuntu
    await app.set_config(
        {
            'reservation': 'affinity',
            'cpu-range': '1,2,3,4',
            'governor': 'performance',
            'raid-autodetection': ''
        }
    )

    await model.block_until(
        lambda: app.status == 'active',
        timeout=TIMEOUT
    )

    principal_app_name = PRINCIPAL_APP_NAME.format(series)
    principal_app = model.applications[principal_app_name]

    await app.destroy_relation('juju-info', '{}:juju-info'.format(principal_app_name))

    await model.block_until(
        lambda: len(app.units) == 0,
        timeout=TIMEOUT
    )

    unit = principal_app.units[0]
    grup_path = '/etc/default/grub.d/90-sysconfig.cfg'
    cmd = 'cat {}'.format(grup_path)
    results = await jujutools.run_command(cmd, unit)
    assert results['Code'] != '0'

    systemd_path = '/etc/systemd/system.conf'
    systemd_content = await jujutools.file_contents(systemd_path, unit)
    assert 'CPUAffinity=1,2,3,4' not in systemd_content

    cpufreq_path = '/etc/default/cpufrequtils'
    cpufreq_content = await jujutools.file_contents(cpufreq_path, unit)
    assert 'GOVERNOR' not in cpufreq_content