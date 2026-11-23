{
  pkgs,
  lib,
  shb,
}:
let
  config =
    (import "${pkgs.path}/nixos/lib/eval-config.nix" {
      inherit pkgs;
      system = pkgs.stdenv.hostPlatform.system;
      specialArgs = { inherit shb; };
      modules = [
        ../../modules/services/immich.nix
        {
          shb.immich = {
            domain = "example.com";
            subdomain = "immich";
            mediaLocation = "/tmp/agent/codex/immich-restore-test";
            restoreTimeout = 8; # Short test-only completion deadline.
            backupApiKey.contract.result.path = "/tmp/agent/codex/immich-restore-test/api-key";
          };
        }
      ];
    }).config;

  restore = pkgs.writeShellApplication {
    name = "immich-restore-test";
    text = lib.concatStringsSep "\n" config.shb.immich.backup.request.afterRestore;
  };
in
pkgs.runCommand "immich-restore-tests"
  {
    nativeBuildInputs = [
      pkgs.python3
      pkgs.ruff
      pkgs.pyrefly
    ];
  }
  ''
    cp ${./immich-restore.py} immich-restore.py
    ruff format --check immich-restore.py
    ruff check immich-restore.py
    pyrefly check immich-restore.py
    export IMMICH_RESTORE_HOOK=${lib.getExe restore}
    python immich-restore.py
    touch "$out"
  ''
