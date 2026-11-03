"""Run at deployment BUILD time after installing argostranslate.
Installs English-to-French/Spanish/Indonesian/German/Italian models.
"""
import argostranslate.package
import argostranslate.translate

TARGETS = ("fr", "es", "id", "de", "it")

print("Updating Argos model index...", flush=True)
argostranslate.package.update_package_index()
packages = argostranslate.package.get_available_packages()

installed = {language.code: language for language in argostranslate.translate.get_installed_languages()}
for target in TARGETS:
    en = installed.get("en")
    destination = installed.get(target)
    if en and destination:
        try:
            en.get_translation(destination)
            print(f"en->{target}: already installed", flush=True)
            continue
        except Exception:
            pass
    candidates = [p for p in packages if p.from_code == "en" and p.to_code == target]
    if not candidates:
        raise RuntimeError(f"No Argos package found for en->{target}. Deployment stopped.")
    package = candidates[0]
    print(f"Downloading and installing en->{target}...", flush=True)
    argostranslate.package.install_from_path(package.download())
    installed = {language.code: language for language in argostranslate.translate.get_installed_languages()}
print("All five offline translation models installed.", flush=True)
