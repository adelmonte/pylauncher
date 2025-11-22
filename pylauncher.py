#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GdkPixbuf, Gio, GLib
import json
import subprocess
import sys
import os
import signal
from pathlib import Path
from collections import defaultdict

# Suppress the GioUnix deprecation warning
import warnings
warnings.filterwarnings('ignore', category=DeprecationWarning)

FAVORITES_FILE = Path.home() / ".config" / "launcher-favorites.json"
LOCK_FILE = Path("/tmp/pylauncher.lock")


class AppLauncher(Gtk.Window):
    
    def __init__(self):
        super().__init__(title="Applications")
        self.set_role("pylauncher")
        self.set_default_size(250, 400)
        self.set_decorated(False)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        
        self.favorites = self.load_favorites()
        self.all_apps = self.load_applications()
        self.categories = self.organize_by_category()
        
        self.dragging = False
        self.drag_row = None
        self.last_hovered_row = None
        self.focus_out_timeout = None
        
        # Navigation state
        self.view_stack = []  # Stack to track navigation: ('favorites',), ('categories',), ('category', 'Games'), ('apps', [...])
        
        self.build_ui()
        self.apply_css()
        self.connect("focus-out-event", self.on_focus_out)
        self.connect("focus-in-event", self.on_focus_in)
        self.connect("key-press-event", self.on_key_press)
        
        self.show_favorites_view()

    
    def apply_css(self):
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            label {
                color: white;
                text-shadow: 
                    0px 1px 2px rgba(0, 0, 0, 0.9),
                    0px 1px 4px rgba(0, 0, 0, 0.6);
            }
            scrollbar {
                opacity: 0;
            }
            .menu-separator {
                background: rgba(255, 255, 255, 0.2);
                min-height: 1px;
                margin: 2px 0;
            }
        """)
        screen = Gdk.Screen.get_default()
        style_context = Gtk.StyleContext()
        style_context.add_provider_for_screen(
            screen, 
            css_provider, 
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    
    def build_ui(self):
        self.main_vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(self.main_vbox)
        
        # Content area (will be rebuilt for each view)
        self.content_scrolled = Gtk.ScrolledWindow()
        self.content_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.main_vbox.pack_start(self.content_scrolled, True, True, 0)
        
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self.on_row_activated)
        self.listbox.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.listbox.connect("motion-notify-event", self.on_listbox_motion)
        self.listbox.connect("leave-notify-event", self.on_listbox_leave)
        self.content_scrolled.add(self.listbox)
        
        # Bottom section with navigation button
        separator = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator.get_style_context().add_class("menu-separator")
        self.main_vbox.pack_start(separator, False, False, 0)
        
        # Navigation button (changes based on view)
        self.nav_button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.main_vbox.pack_start(self.nav_button_box, False, False, 0)
        
        separator2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        separator2.get_style_context().add_class("menu-separator")
        self.main_vbox.pack_start(separator2, False, False, 0)
        
        # Search box
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        search_box.set_margin_start(5)
        search_box.set_margin_end(5)
        search_box.set_margin_top(5)
        search_box.set_margin_bottom(5)
        
        self.search_entry = Gtk.Entry()
        self.search_entry.set_placeholder_text("  Search or $ bash...")
        self.search_entry.connect("changed", self.on_search_changed)
        self.search_entry.connect("activate", self.on_search_activate)
        self.search_entry.connect("key-press-event", self.on_search_entry_key_press)
        search_box.pack_start(self.search_entry, True, True, 0)
        
        self.main_vbox.pack_start(search_box, False, False, 0)

    def on_search_entry_key_press(self, entry, event):
        if event.keyval == Gdk.KEY_Down:
            first_row = self.listbox.get_row_at_index(0)
            if first_row:
                self.listbox.select_row(first_row)
                first_row.grab_focus()
            return False  # Let the event propagate
        return False
    
    def rebuild_nav_button(self, button_type, label_text, icon_name, callback):
        """Rebuild the navigation button"""
        # Clear existing button
        for child in self.nav_button_box.get_children():
            self.nav_button_box.remove(child)
        
        event_box = Gtk.EventBox()
        event_box.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | 
                            Gdk.EventMask.LEAVE_NOTIFY_MASK |
                            Gdk.EventMask.BUTTON_PRESS_MASK)
        
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hbox.set_margin_start(5)
        hbox.set_margin_end(5)
        hbox.set_margin_top(5)
        hbox.set_size_request(-1, 30)
        
        # Icon
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR)
        hbox.pack_start(icon, False, False, 0)
        
        # Label
        label = Gtk.Label(label=label_text)
        label.set_xalign(0.0)
        hbox.pack_start(label, True, True, 0)
        
        # Arrow (for forward navigation)
        if button_type == "forward":
            arrow = Gtk.Image.new_from_icon_name("pan-end-symbolic", Gtk.IconSize.BUTTON)
            hbox.pack_start(arrow, False, False, 0)
#        elif button_type == "back":
#            arrow = Gtk.Image.new_from_icon_name("pan-start-symbolic", Gtk.IconSize.BUTTON)
#            hbox.pack_start(arrow, False, False, 0)
        
        event_box.add(hbox)
        event_box.connect("button-press-event", lambda w, e: callback())
        
        self.nav_button_box.pack_start(event_box, True, True, 0)
        self.nav_button_box.show_all()

    
    def show_favorites_view(self):
        """Show the favorites view with 'All Applications' button"""
        self.view_stack = [('favorites',)]
        
        # Clear listbox
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        
        # Add favorites
        for desktop_id in self.favorites:
            app = next((a for a in self.all_apps if a['desktop_id'] == desktop_id), None)
            if app:
                row = self.create_app_row(app, is_favorite=True, draggable=True)
                self.listbox.add(row)
        
        # Update navigation button
        self.rebuild_nav_button("forward", "All Applications", "folder", self.show_categories_view)
        
        self.listbox.show_all()
        first_row = self.listbox.get_row_at_index(0)
        if first_row:
            self.listbox.select_row(first_row)

    
    def show_categories_view(self):
        """Show all application categories"""
        self.view_stack.append(('categories',))
        
        # Clear listbox
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        # Add "All Applications" entry first
        all_apps_row = self.create_category_row(
            "All Applications",
            "applications-other",
            self.all_apps
        )
        self.listbox.add(all_apps_row)
        
        # Get category icons
        category_icons = {
            'Multimedia': 'applications-multimedia',
            'Development': 'applications-development',
            'Education': 'applications-science',
            'Games': 'applications-games',
            'Graphics': 'applications-graphics',
            'Internet': 'applications-internet',
            'Office': 'applications-office',
            'Science': 'applications-science',
            'Settings': 'preferences-system',
            'System Tools': 'applications-system',
            'Accessories': 'applications-accessories',
            'Other': 'applications-other',
        }
        
        # Add category rows
        for category in sorted(self.categories.keys()):
            apps = self.categories[category]
            if apps:
                row = self.create_category_row(
                    category, 
                    category_icons.get(category, 'folder'),
                    apps
                )
                self.listbox.add(row)
        
        # Update navigation button to Back
        self.rebuild_nav_button("back", "Back", "go-previous", self.go_back)
        
        self.listbox.show_all()
        first_row = self.listbox.get_row_at_index(0)
        if first_row:
            self.listbox.select_row(first_row)

    
    def show_category_apps(self, category_name, apps):
        """Show all apps in a category"""
        self.view_stack.append(('category', category_name, apps))
        
        # Clear listbox
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        
        # Add app rows
        for app in sorted(apps, key=lambda x: x['name'].lower()):
            is_fav = app['desktop_id'] in self.favorites
            row = self.create_app_row(app, is_favorite=is_fav, draggable=False)
            self.listbox.add(row)
        
        # Update navigation button to Back
        self.rebuild_nav_button("back", f"Back", "go-previous", self.go_back)
        
        self.listbox.show_all()
        first_row = self.listbox.get_row_at_index(0)
        if first_row:
            self.listbox.select_row(first_row)

    
    def show_search_results(self, query):
        """Show search results"""
        # Clear listbox
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        
        apps_to_show = self.search_apps(query)[:20]
        
        for app in apps_to_show:
            is_fav = app['desktop_id'] in self.favorites
            row = self.create_app_row(app, is_fav, draggable=False)
            self.listbox.add(row)
        
        self.listbox.show_all()
        first_row = self.listbox.get_row_at_index(0)
        if first_row:
            self.listbox.select_row(first_row)

    
    def go_back(self):
        """Navigate back in the view stack"""
        if len(self.view_stack) > 1:
            self.view_stack.pop()
            previous_view = self.view_stack[-1]
            
            if previous_view[0] == 'favorites':
                # Remove this from stack since show_favorites_view adds it
                self.view_stack.pop()
                self.show_favorites_view()
            elif previous_view[0] == 'categories':
                self.view_stack.pop()
                self.show_categories_view()
            elif previous_view[0] == 'category':
                self.view_stack.pop()
                self.show_category_apps(previous_view[1], previous_view[2])

    
    def create_category_row(self, category_name, icon_name, apps):
        """Create a row for a category"""
        row = Gtk.ListBoxRow()
        row.category_name = category_name
        row.category_apps = apps
        row.is_category = True
        
        event_box = Gtk.EventBox()
        
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hbox.set_margin_start(5)
        hbox.set_margin_end(5)
        hbox.set_margin_top(5)
        hbox.set_margin_bottom(5)
        hbox.set_size_request(-1, 28)
        
        icon = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.LARGE_TOOLBAR)
        hbox.pack_start(icon, False, False, 0)
        
        label = Gtk.Label(label=category_name)
        label.set_xalign(0.0)
        hbox.pack_start(label, True, True, 0)
        
        arrow = Gtk.Image.new_from_icon_name("pan-end-symbolic", Gtk.IconSize.BUTTON)
        hbox.pack_start(arrow, False, False, 0)
        
        event_box.add(hbox)
        row.add(event_box)

        event_box.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        event_box.connect("enter-notify-event", self.on_row_enter, row)
        event_box.connect("leave-notify-event", self.on_row_leave, row)        

        return row

    
    def create_app_row(self, app, is_favorite, draggable=False):
        row = Gtk.ListBoxRow()
        row.app_data = app
        row.is_hovered = False
        row.is_category = False
        
        event_box = Gtk.EventBox()
        
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        hbox.set_margin_start(5)
        hbox.set_margin_end(5)
        hbox.set_margin_top(3)
        hbox.set_margin_bottom(3)
        
        icon = self.create_icon(app)
        hbox.pack_start(icon, False, False, 0)
        
        label = Gtk.Label(label=app['name'])
        label.set_xalign(0.0)
        label.set_ellipsize(3)
        
        tooltip = app['name']
        if app['description']:
            tooltip += f"\n{app['description']}"
        label.set_tooltip_text(tooltip)
        
        hbox.pack_start(label, True, True, 0)
        
        fav_button = Gtk.Button()
        fav_icon_name = "starred" if is_favorite else "non-starred"
        fav_button.set_image(Gtk.Image.new_from_icon_name(fav_icon_name, Gtk.IconSize.BUTTON))
        fav_button.set_relief(Gtk.ReliefStyle.NONE)
        fav_button.set_tooltip_text("Remove from favorites" if is_favorite else "Add to favorites")
        fav_button.connect("clicked", self.on_favorite_clicked, app)
        hbox.pack_start(fav_button, False, False, 0)
        
        event_box.add(hbox)
        row.add(event_box)
        
        if draggable:
            event_box.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK | 
                Gdk.EventMask.BUTTON_RELEASE_MASK | 
                Gdk.EventMask.POINTER_MOTION_MASK | 
                Gdk.EventMask.ENTER_NOTIFY_MASK | 
                Gdk.EventMask.LEAVE_NOTIFY_MASK
            )
            event_box.connect("button-press-event", self.on_button_press, row)
            event_box.connect("button-release-event", self.on_button_release, row)
            event_box.connect("motion-notify-event", self.on_motion_notify, row)
            event_box.connect("enter-notify-event", self.on_row_enter, row)
            event_box.connect("leave-notify-event", self.on_row_leave, row)
        else:
            event_box.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
            event_box.connect("enter-notify-event", self.on_row_enter, row)
            event_box.connect("leave-notify-event", self.on_row_leave, row)
        
        return row

    
    def create_icon(self, app):
        icon_widget = Gtk.Image()
        
        if not app['icon']:
            icon_widget.set_from_icon_name("application-x-executable", Gtk.IconSize.DND)
            return icon_widget
        
        scale_factor = self.get_scale_factor()
        target_size = 32
        load_size = target_size * scale_factor
        
        try:
            icon_theme = Gtk.IconTheme.get_default()
            
            if isinstance(app['icon'], Gio.ThemedIcon):
                icon_names = app['icon'].get_names()
                pixbuf = icon_theme.load_icon(icon_names[0], load_size, Gtk.IconLookupFlags.FORCE_SIZE)
                
                surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, scale_factor, None)
                icon_widget.set_from_surface(surface)
                
            elif isinstance(app['icon'], Gio.FileIcon):
                icon_path = app['icon'].get_file().get_path()
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(icon_path, load_size, load_size)
                
                surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, scale_factor, None)
                icon_widget.set_from_surface(surface)
                
            else:
                icon_widget.set_from_icon_name("application-x-executable", Gtk.IconSize.DND)
                
        except Exception as e:
            icon_widget.set_from_icon_name("application-x-executable", Gtk.IconSize.DND)
        
        return icon_widget

    
    def on_row_activated(self, listbox, row):
        """Handle row activation"""
        if hasattr(row, 'is_category') and row.is_category:
            # Navigate to category
            self.show_category_apps(row.category_name, row.category_apps)
        elif not self.dragging:
            # Launch app
            self.launch_app(row.app_data)

    
    def on_row_enter(self, widget, event, row):
        """Handle mouse entering a row"""
        if not self.dragging:
            row.is_hovered = True
            self.last_hovered_row = row
            self.update_row_state(row)
        return False

    
    def on_row_leave(self, widget, event, row):
        """Handle mouse leaving a row"""
        if not self.dragging:
            row.is_hovered = False
            if self.last_hovered_row == row:
                self.last_hovered_row = None
            self.update_row_state(row)
        return False

    
    def update_row_state(self, row):
        """Update row appearance based on hover and selection state"""
        context = row.get_style_context()
        
        if row.is_hovered:
            context.add_class("hover")
            if not self.listbox.has_focus():
                self.listbox.select_row(row)
        else:
            context.remove_class("hover")

    
    def on_listbox_motion(self, widget, event):
        """Handle mouse motion over listbox - used during dragging"""
        if self.dragging and self.drag_row:
            allocation = self.listbox.get_allocation()
            row_at_cursor = self.listbox.get_row_at_y(int(event.y))
            
            if row_at_cursor and row_at_cursor != self.drag_row:
                drag_index = self.drag_row.get_index()
                target_index = row_at_cursor.get_index()
                
                if drag_index != target_index:
                    self.listbox.remove(self.drag_row)
                    self.listbox.insert(self.drag_row, target_index)
                    self.listbox.show_all()
                    self.listbox.select_row(self.drag_row)
        
        return False

    
    def on_listbox_leave(self, widget, event):
        """Handle mouse leaving listbox"""
        if self.last_hovered_row:
            self.last_hovered_row.is_hovered = False
            self.update_row_state(self.last_hovered_row)
            self.last_hovered_row = None
        return False

    
    def on_button_press(self, widget, event, row):
        if event.button == 1:
            self.dragging = True
            self.drag_row = row
            self.listbox.select_row(row)
        return False

    
    def on_motion_notify(self, widget, event, row):
        """This passes motion events up to the listbox handler"""
        if self.dragging:
            x, y = widget.translate_coordinates(self.listbox, event.x, event.y)
            
            if x is not None and y is not None:
                new_event = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
                new_event.x = x
                new_event.y = y
                self.on_listbox_motion(self.listbox, new_event)
        
        return True

    
    def on_button_release(self, widget, event, row):
        if self.dragging:
            self.dragging = False
            
            new_order = []
            for child in self.listbox.get_children():
                if hasattr(child, 'app_data'):
                    new_order.append(child.app_data['desktop_id'])
            
            self.favorites = new_order
            self.save_favorites()
            self.drag_row = None
        
        return False

    
    def on_favorite_clicked(self, button, app):
        desktop_id = app['desktop_id']
        
        if desktop_id in self.favorites:
            self.favorites.remove(desktop_id)
        else:
            self.favorites.append(desktop_id)
        
        self.save_favorites()
        
        # Refresh current view
        if self.view_stack[-1][0] == 'favorites':
            self.view_stack.pop()
            self.show_favorites_view()
        elif self.view_stack[-1][0] == 'category':
            category_name = self.view_stack[-1][1]
            apps = self.view_stack[-1][2]
            self.view_stack.pop()
            self.show_category_apps(category_name, apps)

    
    def on_search_changed(self, entry):
        query = entry.get_text()
        if query:
            self.show_search_results(query)
        else:
            # Return to previous view
            if self.view_stack and self.view_stack[-1][0] == 'favorites':
                self.view_stack.pop()
                self.show_favorites_view()
            elif len(self.view_stack) > 0:
                current = self.view_stack[-1]
                self.view_stack.pop()
                if current[0] == 'categories':
                    self.show_categories_view()
                elif current[0] == 'category':
                    self.show_category_apps(current[1], current[2])

    
    def on_search_activate(self, entry):
        selected_row = self.listbox.get_selected_row()
        
        if selected_row and hasattr(selected_row, 'app_data'):
            self.launch_app(selected_row.app_data)
            return
        
        children = self.listbox.get_children()
        
        if len(children) == 1 and hasattr(children[0], 'app_data'):
            self.launch_app(children[0].app_data)
            return
        
        query = entry.get_text().strip()
        
        if query:
            try:
                subprocess.Popen(
#                    query,
#                    shell=True,
                    ['fish', '-c', query],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                self.destroy()
            except Exception:
                pass

    
    def search_apps(self, query):
        query = query.lower()
        exact_matches = []
        title_matches = []
        other_matches = []
        
        for app in self.all_apps:
            name_lower = app['name'].lower()
            
            # Check for exact match first
            if name_lower == query:
                exact_matches.append(app)
                continue
            
            # Check if query is in the title
            if query in name_lower:
                title_matches.append(app)
                continue
            
            # Check other fields
            search_text = ' '.join([
                app['description'].lower(),
                app['keywords'],
                app['generic_name']
            ])
            
            if query in search_text:
                other_matches.append(app)
        
        # Combine results with title matches first
        return exact_matches + title_matches + other_matches

    
    def launch_app(self, app):
        methods = [
            lambda: app['app_info'].launch([], None),
            lambda: subprocess.Popen(
                ['gtk-launch', app['desktop_id']],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            ),
            lambda: subprocess.Popen(
                ['dbus-launch', 'gtk-launch', app['desktop_id']],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        ]
        
        for method in methods:
            try:
                method()
                self.destroy()
                return
            except Exception:
                continue
        
        print(f"Failed to launch: {app['name']}")

    
    def organize_by_category(self):
        """Organize applications by their categories"""
        categories = defaultdict(list)
        
        # Category mapping for common categories
        category_names = {
            'AudioVideo': 'Multimedia',
            'Audio': 'Multimedia',
            'Video': 'Multimedia',
            'Development': 'Development',
            'Education': 'Education',
            'Game': 'Games',
            'Graphics': 'Graphics',
            'Network': 'Internet',
            'Office': 'Office',
            'Science': 'Science',
            'Settings': 'Settings',
            'System': 'System Tools',
            'Utility': 'Accessories',
        }
        
        for app in self.all_apps:
            app_info = app['app_info']
            app_categories = app_info.get_categories()
            
            if not app_categories:
                categories['Other'].append(app)
                continue
            
            # Parse categories string
            cat_list = [c.strip() for c in app_categories.split(';') if c.strip()]
            
            # Find the first matching category
            categorized = False
            for cat in cat_list:
                if cat in category_names:
                    categories[category_names[cat]].append(app)
                    categorized = True
                    break
            
            if not categorized:
                categories['Other'].append(app)
        
        return dict(categories)

    
    def load_favorites(self):
        if FAVORITES_FILE.exists():
            try:
                with open(FAVORITES_FILE) as f:
                    return json.load(f)
            except:
                return []
        return []

    
    def save_favorites(self):
        FAVORITES_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(FAVORITES_FILE, 'w') as f:
            json.dump(self.favorites, f, indent=2)

    
    def load_applications(self):
        apps_by_id = {}
        
        search_paths = [
            (Path.home() / ".local/share/applications", 4),
            (Path.home() / ".local/share/flatpak/exports/share/applications", 3),
            (Path("/usr/share/applications"), 2),
            (Path("/var/lib/flatpak/exports/share/applications"), 1),
        ]
        
        for search_path, priority in search_paths:
            if not search_path.exists():
                continue
                
            for desktop_file in search_path.glob("*.desktop"):
                try:
                    app_info = Gio.DesktopAppInfo.new_from_filename(str(desktop_file))
                    
                    if not app_info or app_info.get_nodisplay() or app_info.get_is_hidden():
                        continue
                    
                    name = app_info.get_name()
                    desktop_id = desktop_file.name
                    
                    if not name:
                        continue
                    
                    app_data = {
                        'name': name,
                        'description': app_info.get_description() or '',
                        'icon': app_info.get_icon(),
                        'desktop_id': desktop_id,
                        'desktop_path': str(desktop_file),
                        'app_info': app_info,
                        'keywords': ' '.join(app_info.get_keywords() or []).lower(),
                        'generic_name': (app_info.get_generic_name() or '').lower(),
                        'priority': priority
                    }
                    
                    id_key = desktop_id.lower()
                    
                    if id_key not in apps_by_id or apps_by_id[id_key]['priority'] < priority:
                        apps_by_id[id_key] = app_data
                        
                except Exception:
                    continue
        
        unique_apps = list(apps_by_id.values())
        return sorted(unique_apps, key=lambda x: x['name'].lower())

    
    def on_focus_out(self, widget, event):
        if self.focus_out_timeout:
            GLib.source_remove(self.focus_out_timeout)
        
        self.focus_out_timeout = GLib.timeout_add(500, self.delayed_destroy)
        return False

    
    def on_focus_in(self, widget, event):
        if self.focus_out_timeout:
            GLib.source_remove(self.focus_out_timeout)
            self.focus_out_timeout = None
        return False

    
    def delayed_destroy(self):
        self.destroy()
        return False

    
    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            # Go back or close
            if len(self.view_stack) > 1:
                self.go_back()
            else:
                self.destroy()
            return True
        
        if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            if not self.listbox.has_focus():
                self.listbox.grab_focus()
            return False
        
        if not self.search_entry.has_focus():
            self.search_entry.grab_focus()
        
        return False


def check_single_instance():
    current_pid = os.getpid()
    
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.read().strip())
            
            if pid != current_pid:
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGTERM)
                    LOCK_FILE.unlink()
                    sys.exit(0)
                except OSError:
                    LOCK_FILE.unlink()
        except (ValueError, FileNotFoundError):
            if LOCK_FILE.exists():
                LOCK_FILE.unlink()
    
    with open(LOCK_FILE, 'w') as f:
        f.write(str(current_pid))


def cleanup_lock():
    try:
        LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def signal_handler(sig, frame):
    cleanup_lock()
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    check_single_instance()
    
    win = AppLauncher()
    win.connect("destroy", lambda w: (cleanup_lock(), Gtk.main_quit()))
    win.show_all()
    win.listbox.grab_focus()
    
    Gtk.main()