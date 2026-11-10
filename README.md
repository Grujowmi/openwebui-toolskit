# Open WebUI Toolskit

A collection of plugins for [Open WebUI](https://github.com/open-webui/open-webui), each in its own folder with its own README, guide, changelog and security policy. Every plugin is a single Python file to paste into **Admin Panel > Functions**. No dependency to install.

[Version française plus bas](#français)

## Plugins

| Plugin | Type | What it does | License |
|---|---|---|---|
| [Flash Word](flash-word) | Action | Turns a chat answer into a clean Word document (`.docx`) in one click, with a preview in the chat. No model call. | MIT |
| [Flash Powerpoint](flash-powerpoint) | Action | Turns a chat answer into a ready-to-present PowerPoint deck (`.pptx`), with a slider preview in the chat. | MIT |
| [Diagramme de flux automatique](diagramme-de-flux) | Action | Turns a chat answer describing a process into a flowchart rendered as SVG in the chat, with SVG and PNG download. | MIT |
| [Hélios](H%C3%A9lios%20-%20Token%20usage%20and%20estimated%20cost%20dashboard%20for%20Open%20WebUI) | Function | Admin-only dashboard of token usage and estimated cost per model and per user, with CSV export and an optional email report. Never reads message content. | AGPL-3.0-or-later |

## Compatibility

All plugins were written and tested on **Open WebUI 0.13.0**. The Flash plugins and the flowchart declare 0.11.0 as minimum. Hélios requires PostgreSQL. Details are in each plugin's README.

## Installation

1. Open the plugin folder and copy the content of its `.py` file.
2. In Open WebUI, go to **Admin Panel > Functions**, create a new function, paste, save.
3. Enable the function. For an Action, mark it **Global** (button under every answer) or assign it to specific models.
4. Optional: compare the file's SHA-256 with the one published in the plugin's `CHANGELOG.md`.

Each plugin is a function that runs inside the Open WebUI process. Read the code before installing it, as you would for any third-party function.

## Repository layout

```
.
├── flash-word/
├── flash-powerpoint/
├── diagramme-de-flux/
└── Hélios - Token usage and estimated cost dashboard for Open WebUI/
```

Each folder contains the plugin file, `README.md`, `CHANGELOG.md`, `SECURITY.md`, `LICENSE` and a `docs/` directory.

## Security

Each plugin documents its own security model and how to report a vulnerability in its `SECURITY.md`. The Action plugins generate their files in memory and make no network call of their own.

## License

Licensed per plugin, see the table above and the `LICENSE` file in each folder.

These are independent community projects, not affiliated with or endorsed by Open WebUI.

---

## Français

Collection de plugins pour [Open WebUI](https://github.com/open-webui/open-webui). Chaque plugin a son dossier, avec son README, son guide, son changelog et sa politique de sécurité. Chacun tient dans un seul fichier Python à coller dans **Panneau d'administration > Fonctions**, sans dépendance à installer.

### Plugins

| Plugin | Type | Fonction | Licence |
|---|---|---|---|
| [Flash Word](flash-word) | Action | Transforme une réponse du chat en document Word (`.docx`) propre en un clic, avec aperçu dans le chat. Aucun appel au modèle. | MIT |
| [Flash Powerpoint](flash-powerpoint) | Action | Transforme une réponse en présentation PowerPoint (`.pptx`) prête à projeter, avec aperçu en slider dans le chat. | MIT |
| [Diagramme de flux automatique](diagramme-de-flux) | Action | Transforme une réponse décrivant un processus en logigramme SVG affiché dans le chat, téléchargeable en SVG et PNG. | MIT |
| [Hélios](H%C3%A9lios%20-%20Token%20usage%20and%20estimated%20cost%20dashboard%20for%20Open%20WebUI) | Function | Tableau de bord administrateur de la consommation de tokens et du coût estimé par modèle et par utilisateur, export CSV, rapport par mail optionnel. Ne lit jamais le contenu des messages. | AGPL-3.0 ou ultérieure |

### Compatibilité

Tous les plugins ont été écrits et testés sur **Open WebUI 0.13.0**. Les plugins Flash et le diagramme de flux déclarent 0.11.0 comme minimum. Hélios nécessite PostgreSQL. Le détail est dans le README de chaque plugin.

### Installation

1. Ouvrir le dossier du plugin et copier le contenu de son fichier `.py`.
2. Dans Open WebUI, **Panneau d'administration > Fonctions**, créer une nouvelle fonction, coller, enregistrer.
3. Activer la fonction. Pour une Action, la passer en **globale** (bouton sous chaque réponse) ou l'associer à certains modèles.
4. Facultatif : comparer l'empreinte SHA-256 du fichier avec celle indiquée dans le `CHANGELOG.md` du plugin.

Chaque plugin s'exécute dans le processus d'Open WebUI : lisez le code avant de l'installer, comme pour toute fonction tierce.

### Sécurité

Chaque plugin décrit son modèle de sécurité et la façon de signaler une vulnérabilité dans son `SECURITY.md`. Les Actions génèrent leurs fichiers en mémoire et ne font aucun appel réseau de leur côté.

### Licence

Licence par plugin : voir le tableau ci-dessus et le fichier `LICENSE` de chaque dossier.

Projets communautaires indépendants, non affiliés à Open WebUI et non approuvés par lui.
