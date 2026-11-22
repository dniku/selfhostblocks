# PostgreSQL Block {#blocks-postgresql}

This block sets up a [PostgreSQL][] database.

[postgresql]: https://www.postgresql.org/

Thanks to upstream nixpkgs, SHB supports several versions of PostgreSQL at a time. On top of upstream features, SHB also sets up:

- Enabling TCP/IP login and also accepting password authentication from localhost with [`shb.postgresql.enableTCPIP`](#blocks-postgresql-options-shb.postgresql.enableTCPIP).
- Enhance the `ensure*` upstream option by setting up a database's password from a password file with [`shb.postgresql.ensures`](#blocks-postgresql-options-shb.postgresql.ensures).
- Debug logging with `auto_explain` and `pg_stat_statements` with [`shb.postgresql.debug`](#blocks-postgresql-options-shb.postgresql.debug).
- Automatic backups using the [backup contracts](contracts-databasebackup.html) with [`shb.postgresql.databasebackup`](#blocks-postgresql-options-shb.postgresql.databasebackup).
- Explicit PostgreSQL major-version selection with [`shb.postgresql.version`](#blocks-postgresql-options-shb.postgresql.version).

## Usage {#blocks-postgresql-usage}

It is defined in [`/modules/blocks/postgresql.nix`](@REPO@/modules/blocks/postgresql.nix)
and to use it, add this import:

```nix
{ inputs, ... }:
{
  imports = [ inputs.selfhostblocks.nixosModules.default ];
}
```

### Select a PostgreSQL Version {#blocks-postgresql-version}

SelfHostBlocks requires an explicit PostgreSQL major version:

```nix
shb.postgresql.version = 18;
```

::: {.note}
On first usage of this module, an evaluation error will pop up if this option is not set.
Set it to the major version of PostgreSQL you have already deployed.
Inspect the running server or its `PG_VERSION` file.
:::

Setting the option does not upgrade the database.
See [below](#blocks-postgresql-upgrade) for the upgrade procedure.
If SHB finds a cluster for a different PostgreSQL major version on activation,
it refuses to initialize and reports the required migration steps.

Supported versions are 15, 16, 17, and 18.
The server, data directory, backup tools, and restore tools all follow this selection.

### Declarative User and Database {#blocks-postgresql-ensures}

Ensure a database and user exists:

```nix
shb.postgresql.ensures = [
  {
    username = "firefly-iii";
    database = "firefly-iii";
    passwordFile = "/run/secrets/firefly-iii_db_password";
  }
];
```

### Upgrade PostgreSQL {#blocks-postgresql-upgrade}

[PostgreSQL supports upgrading directly to a newer major version without installing intervening versions](https://www.postgresql.org/docs/18/upgrading.html). SHB provides a helper from the configured version to every newer supported version. For example, version 15 provides `upgrade-pg-cluster-15-16`, `upgrade-pg-cluster-15-17`, and `upgrade-pg-cluster-15-18`.

The following procedure upgrades 15 to 18 and validates the target before applications resume writing. The target configuration is deployed twice: first with consumers held stopped, then with those holds removed. Run shell commands in the same Bash session on the database server; use your normal deployment workflow for configuration changes. Read the migration notes for every intervening PostgreSQL release and verify application and extension compatibility before starting.

#### Prepare the Upgrade {#blocks-postgresql-upgrade-prepare}

Set `shb.postgresql.version = 15;` and deploy so the running source version is explicit and its upgrade helpers are installed. Record the current system path with `readlink -f /run/current-system` and save this SQL output for comparison after migration:

```bash
sudo --user=postgres psql --no-psqlrc --dbname=postgres \
  --set=ON_ERROR_STOP=1 --pset=pager=off --command="
    SHOW server_version;
    SHOW data_directory;
    SHOW data_checksums;
    SELECT datname AS database, pg_get_userbyid(datdba) AS owner
    FROM pg_database
    WHERE NOT datistemplate
    ORDER BY datname;
  "
```

Prepare the target configuration with temporary holds on every database consumer, including application setup/update hooks, scheduled jobs, and backup services and timers. Stopping these units manually before a switch does not keep them stopped across activation.

This example holds Nextcloud, Authelia, Grafana, and two Restic backups. Replace the example Restic instance names `nextcloud` and `postgresql` and the Authelia hostname `auth.example.com` with your configured values. Adapt the unit lists to all consumers on your server. Keep the application configuration in place and disable the systemd units:

```nix
{ config, lib, ... }:
let
  scheduled = [
    "nextcloud-cron"
    "nextcloud-cron-previewgenerator"
    (lib.removeSuffix ".service" config.shb.restic.instances."nextcloud".result.backupService)
    (lib.removeSuffix ".service" config.shb.restic.databases."postgresql".result.backupService)
  ];
in
{
  shb.postgresql.version = 18;

  systemd.services =
    lib.genAttrs
      (
        scheduled
        ++ [
          "phpfpm-nextcloud"
          "nextcloud-setup"
          "nextcloud-update-db"
          "authelia-auth.example.com"
          "grafana"
        ]
      )
      (_: {
        enable = lib.mkForce false;
      });

  systemd.timers = lib.genAttrs scheduled (_: {
    enable = lib.mkForce false;
  });
}
```

Build and dry-activate this target configuration before downtime. Check that the planned activation keeps consumers and scheduled jobs stopped while starting PostgreSQL. Do not switch to it until the upgrade helper has completed successfully.

For the shell commands below, set `pg_backup_unit` and `nextcloud_backup_unit` to the exact backup service names from the currently deployed configuration, including the `.service` suffix. In the example above these are `config.shb.restic.databases."postgresql".result.backupService` and `config.shb.restic.instances."nextcloud".result.backupService`. Adapt these arrays to match the temporary holds, including the Authelia hostname:

```bash
scheduled_services=(
  nextcloud-cron.service
  nextcloud-cron-previewgenerator.service
  "${nextcloud_backup_unit:?Set nextcloud_backup_unit to the Nextcloud backup service}"
  "${pg_backup_unit:?Set pg_backup_unit to the PostgreSQL backup service}"
)
# Each scheduled service in this example has a timer with the same base name.
scheduled_timers=("${scheduled_services[@]/%.service/.timer}")
consumer_services=(
  phpfpm-nextcloud.service
  nextcloud-setup.service
  nextcloud-update-db.service
  authelia-auth.example.com.service
  grafana.service
)
```

#### Back Up and Copy the Cluster {#blocks-postgresql-upgrade-copy}

While PostgreSQL 15 is still running, start a fresh cluster-wide backup and inspect its result:

```bash
sudo systemctl start "$pg_backup_unit" && \
systemctl show "$pg_backup_unit" \
  --property=Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp
```

Require `Result=success`, `ExecMainStatus=0`, and start/exit timestamps for this maintenance run. A success result with empty or old timestamps does not establish a fresh backup. Verify the backup is available through your backup provider.

Stop scheduled timers and any running jobs, put Nextcloud into maintenance mode, and stop application consumers before stopping PostgreSQL:

```bash
sudo systemctl stop "${scheduled_timers[@]}" "${scheduled_services[@]}" && \
sudo nextcloud-occ maintenance:mode --on && \
sudo systemctl stop "${consumer_services[@]}" && \
sudo systemctl stop postgresql.service && \
sudo /run/current-system/sw/bin/pg_controldata /var/lib/postgresql/15
```

`pg_controldata` reads cluster control metadata without starting PostgreSQL. Before proceeding, require this line in its output:

```text
Database cluster state:               shut down
```

If you use filesystem snapshots, this is the point to take and verify a quiescent snapshot as an additional recovery layer. The target path `/var/lib/postgresql/18` must be absent: the helper refuses existing directories, files, and dangling symlinks. If an earlier activation created an empty target directory, inspect it and remove it manually only after confirming it is empty.

Run the installed helper without additional arguments:

```bash
sudo /run/current-system/sw/bin/upgrade-pg-cluster-15-18
```

Require `Upgrade Complete` and exit status zero. The helper performs one upgrade, including `pg_upgrade`'s compatibility checks; it does not support `--check`. It refuses to run while PostgreSQL is active and preserves the standard follow-up output. If it fails, stop and preserve the source, target, and logs for diagnosis. Do not automatically delete the target or retry.

The output includes instructions from PostgreSQL tools that need to be interpreted in the NixOS deployment context:

- **Startup and authentication:** do not run the generic `pg_ctl ... start` command printed by `initdb`. Activate the prepared target configuration in the next phase. The local `trust` warning describes [initdb's generated authentication defaults](https://www.postgresql.org/docs/18/app-initdb.html#APP-INITDB-OPTION-AUTH); the NixOS service [installs the declarative configuration at startup](https://github.com/NixOS/nixpkgs/blob/d4f25a3e562008648d45bcf2d70bcc7422cad84e/nixos/modules/services/databases/postgresql.nix#L781), including its [configured authentication file](https://github.com/NixOS/nixpkgs/blob/d4f25a3e562008648d45bcf2d70bcc7422cad84e/nixos/modules/services/databases/postgresql.nix#L640).
- **Statistics:** save the emitted `vacuumdb` commands to run as `postgres` after starting and verifying the target.
- **Old-cluster deletion:** leave `delete_old_cluster.sh` unexecuted. Cleanup is a separate decision after acceptance.

#### Validate with Consumers Held {#blocks-postgresql-upgrade-validate}

Activate the prepared PostgreSQL 18 configuration through your normal deployment workflow. Check PostgreSQL and all held units:

```bash
systemctl show postgresql.service postgresql-setup.service \
  --property=Id,ActiveState,SubState,Result,ExecMainStatus
systemctl show "${consumer_services[@]}" "${scheduled_services[@]}" "${scheduled_timers[@]}" \
  --property=Id,LoadState,ActiveState,SubState
```

PostgreSQL should be `active/running`, and its setup service `active/exited`, with successful results. Every held unit must report:

```text
LoadState=masked
ActiveState=inactive
SubState=dead
```

Repeat the SQL command from [preparation](#blocks-postgresql-upgrade-prepare). Require server major 18, data directory `/var/lib/postgresql/18`, the same checksum setting recorded on the source, and all expected databases and owners. Checksums may be `on` or `off`: the helper preserves the source setting rather than adopting PostgreSQL 18's new-cluster default.

Run the statistics commands emitted by your target version as the `postgres` operating-system user. For PostgreSQL 18, after activating the target system:

```bash
sudo --user=postgres /run/current-system/sw/bin/vacuumdb \
  --all --analyze-in-stages --missing-stats-only && \
sudo --user=postgres /run/current-system/sw/bin/vacuumdb \
  --all --analyze-only
```

[PostgreSQL 18 recommends both commands](https://www.postgresql.org/docs/18/pgupgrade.html) because not all statistics are transferred. Require both to complete successfully. The second command can print `vacuuming database` even with `--analyze-only`; that progress message is not an error.

#### Resume Consumers and Verify Acceptance {#blocks-postgresql-upgrade-resume}

Remove only the temporary holds, retain `shb.postgresql.version = 18;`, and deploy again. This permits application writes to the new cluster; the old cluster becomes stale as those writes occur.

Verify that application services are running and scheduled timers are restored. Check Nextcloud's status, if used:

```bash
sudo nextcloud-occ status
```

Require `installed: true`, `maintenance: false`, and `needsDbUpgrade: false`. If maintenance mode is still enabled, disable it with `sudo nextcloud-occ maintenance:mode --off` after confirming no database upgrade is pending.

Exercise each application's existing data and a representative operation. For the example consumers:

- **Authelia:** sign in afresh.
- **Grafana:** open an existing dashboard and refresh its data.
- **Nextcloud:** create, reopen, and delete a disposable file.

Run a scheduled job and a fresh PostgreSQL 18 backup, then inspect their results:

```bash
sudo systemctl start nextcloud-cron.service && \
sudo systemctl start "$pg_backup_unit" && \
systemctl show nextcloud-cron.service "$pg_backup_unit" \
  --property=Id,Result,ExecMainStatus,ExecMainStartTimestamp,ExecMainExitTimestamp
```

Require success, zero exit status, and fresh timestamps for both jobs. Review `systemctl --failed` for migration-related failures. After a controlled reboot, repeat the SQL, service/timer, and authenticated application checks and run the scheduled job again before declaring acceptance complete.

#### Recovery and Later Cleanup {#blocks-postgresql-upgrade-recovery}

Run without additional arguments, the helper uses PostgreSQL's default copy mode and preserves the source cluster. Additional arguments are forwarded to `pg_upgrade`; options such as `--link` and `--swap` change these [recovery guarantees](https://www.postgresql.org/docs/18/pgupgrade.html). This procedure assumes the no-argument invocation.

Before applications write to the target, the preserved source and recorded source system provide a rollback path. Keep consumers held while deciding how to recover and stop the target server before reactivating the source. Once applications write to PostgreSQL 18, PostgreSQL 15 is stale: switching back does not preserve those newer writes. Decide how to recover or reconcile them before reverting.

Retain the source cluster and any recovery snapshot until a separate cleanup decision after application, backup, and reboot acceptance. Do not run `delete_old_cluster.sh` or delete `/var/lib/postgresql/15` as part of the upgrade itself. A successful backup job is not evidence that SQL restoration has been tested.

### Database Backup Requester Contracts {#blocks-postgresql-contract-databasebackup}

This block can be backed up using the [database backup](contracts-databasebackup.html) contract.

Contract integration tests are defined in [`/test/contracts/databasebackup.nix`](@REPO@/test/contracts/databasebackup.nix).

#### Backing up All Databases {#blocks-postgresql-contract-databasebackup-all}

```nix
{
  my.backup.provider."postgresql" = {
    request = config.shb.postgresql.databasebackup;

    settings = {
      // Specific options for the backup provider.
    };
  };
}
```

## Tests {#blocks-postgresql-tests}

Specific integration tests are defined in [`/test/blocks/postgresql.nix`](@REPO@/test/blocks/postgresql.nix).

## Options Reference {#blocks-postgresql-options}

```{=include=} options
id-prefix: blocks-postgresql-options-
list-id: selfhostblocks-block-postgresql-options
source: @OPTIONS_JSON@
```
