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
        self.all_apps = []  # Load asynchronously
        self.categories = {}
        self.apps_loaded = False
        self._visible = False

        self.dragging = False
        self.drag_row = None
        self.last_hovered_row = None
        self.focus_out_timeout = None

        # Navigation state
        self.view_stack = []
        
        # Animation state
        self.is_animating = False
        
        self.apply_css()
        self.build_ui()

        # Load apps and populate before showing
        self.all_apps = self.load_applications()
        self.categories = self.organize_by_category()
        self.apps_loaded = True
        self.show_favorites_view()

        # Connect events
        self.connect("focus-out-event", self.on_focus_out)
        self.connect("focus-in-event", self.on_focus_in)
        self.connect("key-press-event", self.on_key_press)
        self.connect("delete-event", self._on_delete_event)

        # Show window fully formed
        self.show_all()
        self._visible = True
        self.listbox.grab_focus()

        # Signal waybar that launcher is active
        self._signal_waybar()

    def _on_delete_event(self, widget, event):
        self.hide_launcher()
        return True

    def toggle_visibility(self):
        if self._visible:
            self.hide_launcher()
        else:
            self.show_launcher()

    def show_launcher(self):
        self.favorites = self.load_favorites()
        # Reset to favorites view
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        self.view_stack = []
        self.search_entry.handler_block_by_func(self.on_search_changed)
        self.search_entry.set_text("")
        self.search_entry.handler_unblock_by_func(self.on_search_changed)
        self.show_favorites_view()
        # Block row activation until stale Wayland key events pass.
        # When a window gains focus, Wayland delivers currently-pressed keys.
        # If Return was recently pressed, we'd get a stray activation.
        self.listbox_1.handler_block_by_func(self.on_row_activated)
        self.listbox_2.handler_block_by_func(self.on_row_activated)
        self.show_all()
        self.present()
        self._visible = True
        # Defer first-row selection so it runs after GTK processes present() focus events
        GLib.idle_add(self._select_first_row)
        self._signal_waybar()
        GLib.timeout_add(150, self._unblock_row_activation)

    def _unblock_row_activation(self):
        self.listbox_1.handler_unblock_by_func(self.on_row_activated)
        self.listbox_2.handler_unblock_by_func(self.on_row_activated)
        return False

    def hide_launcher(self):
        if self.focus_out_timeout:
            GLib.source_remove(self.focus_out_timeout)
            self.focus_out_timeout = None
        self.hide()
        self._visible = False
        self._signal_waybar()

    def _signal_waybar(self):
        with open(LOCK_FILE, 'w') as f:
            f.write(f"{os.getpid()}\n{'visible' if self._visible else 'hidden'}")
        subprocess.run(['pkill', '-RTMIN+8', 'waybar'], stderr=subprocess.DEVNULL)

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
		    /* Normalize listbox row selection/hover colors */
		    list row:selected {
		        background-color: alpha(@theme_selected_bg_color, 0.6);
		    }
		    list row:hover {
		        background-color: alpha(@theme_selected_bg_color, 0.0);
		    }
		    list row:selected:hover {
		        background-color: alpha(@theme_selected_bg_color, 0.7);
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
        
        # Stack for content transitions
        self.content_stack = Gtk.Stack()
        self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.content_stack.set_transition_duration(250)
        
        # Create two scrolled windows for alternating between views
        self.content_scrolled_1 = Gtk.ScrolledWindow()
        self.content_scrolled_1.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox_1 = Gtk.ListBox()
        self.listbox_1.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox_1.connect("row-activated", self.on_row_activated)
        self.listbox_1.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.listbox_1.connect("motion-notify-event", self.on_listbox_motion)
        self.listbox_1.connect("leave-notify-event", self.on_listbox_leave)
        self.content_scrolled_1.add(self.listbox_1)
        
        self.content_scrolled_2 = Gtk.ScrolledWindow()
        self.content_scrolled_2.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox_2 = Gtk.ListBox()
        self.listbox_2.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox_2.connect("row-activated", self.on_row_activated)
        self.listbox_2.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.listbox_2.connect("motion-notify-event", self.on_listbox_motion)
        self.listbox_2.connect("leave-notify-event", self.on_listbox_leave)
        self.content_scrolled_2.add(self.listbox_2)
        
        self.content_stack.add_named(self.content_scrolled_1, "view1")
        self.content_stack.add_named(self.content_scrolled_2, "view2")
        
        # Start with view1
        self.current_view = "view1"
        self.listbox = self.listbox_1
        self.content_scrolled = self.content_scrolled_1
        
        self.main_vbox.pack_start(self.content_stack, True, True, 0)
        
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
        self.search_entry.set_placeholder_text("  Search or $ shell...")
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
            return False
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
        
        # Add hand cursor on hover
        hand_cursor = Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "pointer")
        event_box.connect("enter-notify-event", lambda w, e: w.get_window().set_cursor(hand_cursor) if w.get_window() else None)
        event_box.connect("leave-notify-event", lambda w, e: w.get_window().set_cursor(None) if w.get_window() else None)
        
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
        
        event_box.add(hbox)
        event_box.connect("button-press-event", lambda w, e: callback())
        
        self.nav_button_box.pack_start(event_box, True, True, 0)
        self.nav_button_box.show_all()

    
    def animate_transition(self, direction, populate_func):
        """Animate transition between views
        direction: 'forward' or 'back'
        populate_func: function to populate the new view
        """
        if self.is_animating:
            return
        
        self.is_animating = True
        
        # Switch to the other view
        if self.current_view == "view1":
            next_view = "view2"
            next_listbox = self.listbox_2
            next_scrolled = self.content_scrolled_2
        else:
            next_view = "view1"
            next_listbox = self.listbox_1
            next_scrolled = self.content_scrolled_1
        
        # Populate the next view
        for child in next_listbox.get_children():
            next_listbox.remove(child)
        
        populate_func(next_listbox)
        next_listbox.show_all()
        
        # Set transition direction
        if direction == 'forward':
            self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT)
        else:
            self.content_stack.set_transition_type(Gtk.StackTransitionType.SLIDE_RIGHT)
        
        # Switch to the new view
        self.content_stack.set_visible_child_name(next_view)
        
        # Update current references
        self.current_view = next_view
        self.listbox = next_listbox
        self.content_scrolled = next_scrolled
        
        # Select first row after animation
        GLib.timeout_add(260, self._post_animation_setup)
    
    def _post_animation_setup(self):
        """Called after animation completes"""
        self.is_animating = False
        GLib.idle_add(self._select_first_row)
        return False

    def _select_first_row(self):
        """Select and keyboard-focus the first row in the current listbox."""
        first_row = self.listbox.get_row_at_index(0)
        if first_row:
            self.listbox.select_row(first_row)
            # Don't steal focus from the search entry mid-typing
            if not self.search_entry.has_focus():
                first_row.grab_focus()
        return False
    
    def show_favorites_view(self, animate=False, direction='back'):
        """Show the favorites view with 'All Applications' button"""
        self.view_stack = [('favorites',)]
        
        def populate(listbox):
            # Add favorites
            for desktop_id in self.favorites:
                app = next((a for a in self.all_apps if a['desktop_id'] == desktop_id), None)
                if app:
                    row = self.create_app_row(app, is_favorite=True, draggable=True)
                    listbox.add(row)
            
            # Update navigation button
            self.rebuild_nav_button("forward", "All Applications", "folder", self.show_categories_view)
        
        if animate:
            self.animate_transition(direction, populate)
        else:
            populate(self.listbox)
            self.listbox.show_all()
            GLib.idle_add(self._select_first_row)

    
    def show_categories_view(self, direction='forward'):
        """Show all application categories"""
        self.view_stack.append(('categories',))
        
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
        
        def populate(listbox):
            # Add "All Applications" entry first
            all_apps_row = self.create_category_row(
                "All Applications",
                "applications-other",
                self.all_apps
            )
            listbox.add(all_apps_row)
            
            # Add category rows
            for category in sorted(self.categories.keys()):
                apps = self.categories[category]
                if apps:
                    row = self.create_category_row(
                        category, 
                        category_icons.get(category, 'folder'),
                        apps
                    )
                    listbox.add(row)
            
            # Update navigation button to Back
            self.rebuild_nav_button("back", "Back", "go-previous", self.go_back)
        
        self.animate_transition(direction, populate)

    
    def show_category_apps(self, category_name, apps, direction='forward', animate=True):
        """Show all apps in a category"""
        self.view_stack.append(('category', category_name, apps))
        
        def populate(listbox):
            # Add app rows
            for app in sorted(apps, key=lambda x: x['name'].lower()):
                is_fav = app['desktop_id'] in self.favorites
                row = self.create_app_row(app, is_favorite=is_fav, draggable=False)
                listbox.add(row)
            
            # Update navigation button to Back
            self.rebuild_nav_button("back", f"Back", "go-previous", self.go_back)
        
        if animate:
            self.animate_transition(direction, populate)
        else:
            # Non-animated version for refreshes
            for child in self.listbox.get_children():
                self.listbox.remove(child)
            populate(self.listbox)
            self.listbox.show_all()
            GLib.idle_add(self._select_first_row)

    
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
        GLib.idle_add(self._select_first_row)


    def go_back(self):
        """Navigate back in the view stack"""
        if len(self.view_stack) > 1:
            self.view_stack.pop()
            previous_view = self.view_stack[-1]
            
            if previous_view[0] == 'favorites':
                self.view_stack.pop()
                self.show_favorites_view(animate=True, direction='back')
            elif previous_view[0] == 'categories':
                self.view_stack.pop()
                self.show_categories_view(direction='back')
            elif previous_view[0] == 'category':
                self.view_stack.pop()
                self.show_category_apps(previous_view[1], previous_view[2], direction='back')
    
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
            event_box.add_events(
                Gdk.EventMask.BUTTON_PRESS_MASK |
                Gdk.EventMask.ENTER_NOTIFY_MASK |
                Gdk.EventMask.LEAVE_NOTIFY_MASK
            )
            event_box.connect("button-press-event", self.on_button_press, row)
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
            self.listbox.select_row(row)

    
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
        if event.button == 3:
            self.show_context_menu(row.app_data, event)
            return True
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

    
    def show_context_menu(self, app, event):
        menu = Gtk.Menu()

        launch_item = Gtk.MenuItem(label=f"Launch {app['name']}")
        launch_item.connect("activate", lambda item: self.launch_app(app))
        menu.append(launch_item)

        menu.append(Gtk.SeparatorMenuItem())

        open_location_item = Gtk.MenuItem(label="Open .desktop file location")
        open_location_item.connect(
            "activate",
            lambda item, p=app['desktop_path']: self._open_file_location(p)
        )
        menu.append(open_location_item)

        menu.show_all()
        menu.popup_at_pointer(event)

    def _open_file_location(self, desktop_path):
        directory = str(Path(desktop_path).parent)
        try:
            subprocess.Popen(
                ['xdg-open', directory],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        except Exception:
            pass

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
            self.show_category_apps(category_name, apps, animate=False)

    
    def on_search_changed(self, entry):
        query = entry.get_text()
        if query:
            self.show_search_results(query)
        else:
            # Return to current view from view_stack
            self.restore_current_view()

    def restore_current_view(self):
        """Restore the current view after clearing search"""
        if not self.view_stack:
            self.show_favorites_view()
            return

        current = self.view_stack[-1]

        # Clear current listbox
        for child in self.listbox.get_children():
            self.listbox.remove(child)

        if current[0] == 'favorites':
            # Repopulate with favorites
            for desktop_id in self.favorites:
                app = next((a for a in self.all_apps if a['desktop_id'] == desktop_id), None)
                if app:
                    row = self.create_app_row(app, is_favorite=True, draggable=True)
                    self.listbox.add(row)
            self.rebuild_nav_button("forward", "All Applications", "folder", self.show_categories_view)

        elif current[0] == 'categories':
            # Repopulate with categories
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

            # Add "All Applications" entry first
            all_apps_row = self.create_category_row("All Applications", "applications-other", self.all_apps)
            self.listbox.add(all_apps_row)

            for category in sorted(self.categories.keys()):
                apps = self.categories[category]
                if apps:
                    row = self.create_category_row(category, category_icons.get(category, 'folder'), apps)
                    self.listbox.add(row)

            self.rebuild_nav_button("back", "Back", "go-previous", self.go_back)

        elif current[0] == 'category':
            # Repopulate with category apps
            category_name, apps = current[1], current[2]
            for app in sorted(apps, key=lambda x: x['name'].lower()):
                is_fav = app['desktop_id'] in self.favorites
                row = self.create_app_row(app, is_favorite=is_fav, draggable=False)
                self.listbox.add(row)
            self.rebuild_nav_button("back", "Back", "go-previous", self.go_back)

        self.listbox.show_all()
        GLib.idle_add(self._select_first_row)


    def on_search_activate(self, entry):
        query = entry.get_text().strip()

        if not query:
            return

        selected_row = self.listbox.get_selected_row()

        if selected_row and hasattr(selected_row, 'app_data'):
            self.launch_app(selected_row.app_data)
            return

        children = self.listbox.get_children()

        if len(children) == 1 and hasattr(children[0], 'app_data'):
            self.launch_app(children[0].app_data)
            return
        
        if query:
            try:
                subprocess.Popen(
                    ['fish', '-c', query],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True
                )
                self.hide_launcher()
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
                self.hide_launcher()
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
                    desktop_id = desktop_file.name
                    id_key = desktop_id.lower()
                    
                    # If we already processed this desktop_id with higher priority, skip
                    if id_key in apps_by_id and apps_by_id[id_key]['priority'] >= priority:
                        continue
                    
                    app_info = Gio.DesktopAppInfo.new_from_filename(str(desktop_file))
                    
                    # Check if hidden or nodisplay
                    if not app_info or app_info.get_nodisplay() or app_info.get_is_hidden():
                        # Mark as hidden so lower priority versions don't show up
                        apps_by_id[id_key] = {
                            'hidden': True,
                            'priority': priority
                        }
                        continue
                    
                    name = app_info.get_name()
                    
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
                        'priority': priority,
                        'hidden': False
                    }
                    
                    apps_by_id[id_key] = app_data
                
                except Exception:
                    continue
        
        # Filter out hidden entries
        unique_apps = [app for app in apps_by_id.values() if not app.get('hidden', False)]
        return sorted(unique_apps, key=lambda x: x['name'].lower())

    
    def on_focus_out(self, widget, event):
        if self.focus_out_timeout:
            GLib.source_remove(self.focus_out_timeout)

        self.focus_out_timeout = GLib.timeout_add(500, self._delayed_hide)
        return False


    def on_focus_in(self, widget, event):
        if self.focus_out_timeout:
            GLib.source_remove(self.focus_out_timeout)
            self.focus_out_timeout = None
        if not self.search_entry.get_text():
            self.listbox.grab_focus()
        return False


    def _delayed_hide(self):
        self.hide_launcher()
        return False

    
    def on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            # Go back or close
            if len(self.view_stack) > 1:
                self.go_back()
            else:
                self.hide_launcher()
            return True

        if event.keyval in (Gdk.KEY_Up, Gdk.KEY_Down):
            if not self.listbox.has_focus():
                self.listbox.grab_focus()
            return False

        # Let Return reach the focused widget naturally (listbox or search entry)
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            return False

        if not self.search_entry.has_focus():
            self.search_entry.grab_focus()
            self.search_entry.set_position(-1)

        return False


def check_single_instance():
    current_pid = os.getpid()
    
    if LOCK_FILE.exists():
        try:
            with open(LOCK_FILE) as f:
                pid = int(f.readline().strip())
            
            if pid != current_pid:
                try:
                    os.kill(pid, 0)
                    os.kill(pid, signal.SIGUSR1)
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
    # Signal waybar that launcher is inactive
    subprocess.run(['pkill', '-RTMIN+8', 'waybar'], stderr=subprocess.DEVNULL)


_launcher = None


def signal_handler(sig, frame):
    cleanup_lock()
    Gtk.main_quit()


def on_toggle_signal(sig, frame):
    if _launcher:
        GLib.idle_add(_launcher.toggle_visibility)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGUSR1, on_toggle_signal)
    check_single_instance()

    _launcher = AppLauncher()

    Gtk.main()
    cleanup_lock()
