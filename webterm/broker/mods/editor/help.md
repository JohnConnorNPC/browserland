The text editor is a CodeMirror-backed editor with syntax highlighting, used to view and edit files. It opens on the **active terminal's host**, at the Control Panel **Default start path** when one is set for that host — otherwise at the active terminal's working directory.

The editor's content is backed by a real file on the host. Closing the editor with unsaved changes prompts you to save first; the editor window itself is not kept around, but your file on disk is safe once written.
