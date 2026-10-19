from odoo import api, fields, models


class LibraryAuthor(models.Model):
    _name = 'library.author'
    _description = 'Author'
    _order = 'name'

    name = fields.Char(required=True)
    partner_id = fields.Many2one('res.partner')
    book_ids = fields.One2many('library.book', 'author_id')
    book_count = fields.Integer(compute='_compute_book_count')

    @api.depends('book_ids')
    def _compute_book_count(self):
        for rec in self:
            rec.book_count = len(rec.book_ids)


class LibraryBook(models.Model):
    _name = 'library.book'
    _description = 'Book'
    _rec_name = 'title'

    title = fields.Char(required=True)
    author_id = fields.Many2one('library.author')
    author_country_id = fields.Many2one(related='author_id.partner_id.country_id')
    state = fields.Selection([('draft', 'Draft'), ('done', 'Done')], default='draft')
    company_id = fields.Many2one('res.company')
    currency_id = fields.Many2one(related='company_id.currency_id')
    price = fields.Monetary(currency_field='currency_id')
    isbn = fields.Char()

    _sql_constraints = [('isbn_uniq', 'unique(isbn)', 'ISBN must be unique')]

    @api.constrains('isbn')
    def _check_isbn(self):
        pass

    def action_done(self):
        self.write({'state': 'done'})
