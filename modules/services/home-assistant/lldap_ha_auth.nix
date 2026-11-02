{
  lib,
  pkgs,
  stdenvNoCC,
}:
stdenvNoCC.mkDerivation {
  name = "lldap-ha-auth";

  src = pkgs.fetchFromGitHub {
    owner = "ibizaman";
    repo = "lldap";
    rev = "888a75c21e30b66ee7037bedefad2b49313216bd";
    sha256 = "sha256-d9ZCfNoI6yRHDgxZF9ZQAuyMeWs49OwuUO/0b5o/tcU=";
  };

  # Remove when upstream encodes credentials as JSON instead of interpolating them:
  # https://github.com/ibizaman/lldap/blob/888a75c21e30b66ee7037bedefad2b49313216bd/example_configs/lldap-ha-auth.sh#L53
  patches = [ ./lldap-ha-auth-json.patch ];

  nativeBuildInputs = [
    pkgs.makeWrapper
  ];

  buildPhase = ''
    mkdir -p $out/bin

    cp example_configs/lldap-ha-auth.sh $out/bin/lldap-ha-auth
    chmod a+x $out/bin/lldap-ha-auth
  '';

  installPhase = ''
    wrapProgram $out/bin/lldap-ha-auth \
      --prefix PATH : ${
        lib.makeBinPath [
          pkgs.gnused
          pkgs.curl
          pkgs.jq
        ]
      }
  '';
}
