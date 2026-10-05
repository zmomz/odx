from odoo import fields, models


class LibraryBook(models.Model):
    _inherit = 'library.book'

    shelf_code = fields.Char()
