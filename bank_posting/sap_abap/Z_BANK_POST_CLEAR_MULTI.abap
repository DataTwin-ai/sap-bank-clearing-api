FUNCTION z_bank_post_clear_multi
  IMPORTING
    VALUE(iv_bukrs) TYPE bukrs
    VALUE(iv_kunnr) TYPE kunnr
    VALUE(iv_bldat) TYPE bldat
    VALUE(iv_budat) TYPE budat
    VALUE(iv_valut) TYPE valut OPTIONAL
    VALUE(iv_blart) TYPE blart DEFAULT 'DZ'
    VALUE(iv_monat) TYPE monat OPTIONAL
    VALUE(iv_waers) TYPE waers
    VALUE(iv_hkont) TYPE hkont
    VALUE(iv_wrbtr) TYPE wrbtr
    VALUE(iv_match_by) TYPE fieldname DEFAULT 'XBLNR'
    VALUE(iv_invoice_refs) TYPE string
    VALUE(iv_utr) TYPE xblnr1 OPTIONAL
    VALUE(iv_zuonr) TYPE dzuonr OPTIONAL
    VALUE(iv_sgtxt) TYPE sgtxt OPTIONAL
  EXPORTING
    VALUE(ev_status) TYPE char1
    VALUE(ev_belnr) TYPE belnr_d
    VALUE(ev_gjahr) TYPE gjahr
    VALUE(ev_augbl) TYPE augbl
    VALUE(ev_augdt) TYPE augdt
    VALUE(ev_cleared) TYPE int4
    VALUE(ev_message) TYPE bapi_msg.
*----------------------------------------------------------------------
* Bank receipt posting WITH clearing of SEVERAL customer open items:
* one bank receipt <-> one DataTwin connection (public.active_cnx) with
* 1..n invoices. One FB05 document posts the bank line and clears all
* invoices, so the posting document is also the clearing document.
*
* IV_INVOICE_REFS: invoice references separated by ',' and matched on
* IV_MATCH_BY (XBLNR / ZUONR / BELNR). The caller checks that the bank
* amount equals the sum of the open items (no residual / partial).
*
* RFC-enabled; called by DataTwin via /sap/bc/soap/rfc.
* EV_STATUS : S = posted, every invoice cleared
*             W = posted, not every invoice found cleared in BSAD
*             E = error, nothing posted
* EV_CLEARED: number of invoice references found cleared by EV_BELNR
*----------------------------------------------------------------------

  TYPES ty_ref TYPE c LENGTH 20.

  DATA: lt_ftpost  TYPE STANDARD TABLE OF ftpost,
        lt_ftclear TYPE STANDARD TABLE OF ftclear,
        lt_fttax   TYPE STANDARD TABLE OF fttax,
        lt_blntab  TYPE STANDARD TABLE OF blntab,
        lt_raw     TYPE STANDARD TABLE OF string WITH EMPTY KEY,
        lt_refs    TYPE STANDARD TABLE OF ty_ref WITH EMPTY KEY,
        ls_blntab  TYPE blntab,
        lv_kunnr   TYPE kunnr,
        lv_raw     TYPE string,
        lv_ref     TYPE ty_ref,
        lv_belnr   TYPE belnr_d,
        lv_count   TYPE i,
        lv_total   TYPE i,
        lv_date    TYPE char10,
        lv_amount  TYPE char20,
        lv_xblnr   TYPE xblnr1,
        lv_zuonr   TYPE dzuonr,
        lv_missing TYPE string,
        lv_msgid   TYPE sy-msgid,
        lv_msgno   TYPE sy-msgno,
        lv_msgty   TYPE sy-msgty,
        lv_msgv1   TYPE sy-msgv1,
        lv_msgv2   TYPE sy-msgv2,
        lv_msgv3   TYPE sy-msgv3,
        lv_msgv4   TYPE sy-msgv4,
        lv_subrc   TYPE sy-subrc.

  CLEAR: ev_status, ev_belnr, ev_gjahr, ev_augbl, ev_augdt, ev_cleared, ev_message.

  IF iv_match_by <> 'XBLNR' AND iv_match_by <> 'ZUONR' AND iv_match_by <> 'BELNR'.
    ev_status  = 'E'.
    ev_message = |MatchBy { iv_match_by } not supported (XBLNR/ZUONR/BELNR)|.
    RETURN.
  ENDIF.

  CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'
    EXPORTING input  = iv_kunnr
    IMPORTING output = lv_kunnr.

*--------------------------------------------------------------------*
* 0. Invoice references (BELNR padded to 10 digits)
*--------------------------------------------------------------------*
  SPLIT iv_invoice_refs AT ',' INTO TABLE lt_raw.
  LOOP AT lt_raw INTO lv_raw.
    CONDENSE lv_raw.
    IF lv_raw IS INITIAL.
      CONTINUE.
    ENDIF.
    lv_ref = lv_raw.
    IF iv_match_by = 'BELNR'.
      CALL FUNCTION 'CONVERSION_EXIT_ALPHA_INPUT'
        EXPORTING input  = lv_ref
        IMPORTING output = lv_belnr.
      lv_ref = lv_belnr.
    ENDIF.
    APPEND lv_ref TO lt_refs.
  ENDLOOP.
  SORT lt_refs BY table_line.
  DELETE ADJACENT DUPLICATES FROM lt_refs COMPARING table_line.
  lv_total = lines( lt_refs ).
  IF lv_total = 0.
    ev_status  = 'E'.
    ev_message = 'No invoice reference given (IV_INVOICE_REFS)'.
    RETURN.
  ENDIF.

*--------------------------------------------------------------------*
* 1. Duplicate check: same UTR already posted in this company code
*--------------------------------------------------------------------*
  IF iv_utr IS NOT INITIAL.
    SELECT SINGLE belnr FROM bkpf INTO @DATA(lv_dup)
      WHERE bukrs = @iv_bukrs
        AND blart = @iv_blart
        AND xblnr = @iv_utr
        AND stblg = @space.
    IF sy-subrc = 0.
      ev_status  = 'E'.
      ev_message = |UTR { iv_utr } already posted in document { lv_dup }|.
      RETURN.
    ENDIF.
  ENDIF.

*--------------------------------------------------------------------*
* 2. Every invoice must be an open item of this customer
*--------------------------------------------------------------------*
  LOOP AT lt_refs INTO lv_ref.
    CLEAR lv_count.
    CASE iv_match_by.
      WHEN 'XBLNR'.
        SELECT COUNT(*) FROM bsid_view INTO @lv_count
          WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr AND xblnr = @lv_ref.
      WHEN 'ZUONR'.
        SELECT COUNT(*) FROM bsid_view INTO @lv_count
          WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr AND zuonr = @lv_ref.
      WHEN 'BELNR'.
        lv_belnr = lv_ref.
        SELECT COUNT(*) FROM bsid_view INTO @lv_count
          WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr AND belnr = @lv_belnr.
    ENDCASE.
    IF lv_count = 0.
      IF lv_missing IS INITIAL.
        lv_missing = lv_ref.
      ELSE.
        lv_missing = |{ lv_missing }, { lv_ref }|.
      ENDIF.
    ENDIF.
  ENDLOOP.
  IF lv_missing IS NOT INITIAL.
    ev_status  = 'E'.
    ev_message = |No open item for customer { lv_kunnr } with { iv_match_by } = { lv_missing }|.
    RETURN.
  ENDIF.

*--------------------------------------------------------------------*
* 3. Build posting data (values in user format, like batch input)
*--------------------------------------------------------------------*
  READ TABLE lt_refs INTO lv_ref INDEX 1.
  IF iv_utr IS NOT INITIAL.
    lv_xblnr = iv_utr.
  ELSE.
    lv_xblnr = lv_ref.
  ENDIF.
  IF iv_zuonr IS NOT INITIAL.
    lv_zuonr = iv_zuonr.
  ELSEIF iv_utr IS NOT INITIAL.
    lv_zuonr = iv_utr.
  ELSE.
    lv_zuonr = lv_ref.
  ENDIF.

  " Header (K)
  WRITE iv_bldat TO lv_date.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BLDAT' fval = lv_date ) TO lt_ftpost.
  WRITE iv_budat TO lv_date.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BUDAT' fval = lv_date ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BLART' fval = iv_blart ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-BUKRS' fval = iv_bukrs ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-WAERS' fval = iv_waers ) TO lt_ftpost.
  APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-XBLNR' fval = lv_xblnr ) TO lt_ftpost.
  IF iv_monat IS NOT INITIAL.
    APPEND VALUE #( stype = 'K' count = 1 fnam = 'BKPF-MONAT' fval = iv_monat ) TO lt_ftpost.
  ENDIF.

  " Bank line (P) : posting key 40 = debit G/L
  WRITE iv_wrbtr TO lv_amount CURRENCY iv_waers LEFT-JUSTIFIED.
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'RF05A-NEWBS' fval = '40' ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-HKONT' fval = iv_hkont ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-WRBTR' fval = lv_amount ) TO lt_ftpost.
  APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-ZUONR' fval = lv_zuonr ) TO lt_ftpost.
  IF iv_sgtxt IS NOT INITIAL.
    APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-SGTXT' fval = iv_sgtxt ) TO lt_ftpost.
  ENDIF.
  IF iv_valut IS NOT INITIAL.
    WRITE iv_valut TO lv_date.
    APPEND VALUE #( stype = 'P' count = 1 fnam = 'BSEG-VALUT' fval = lv_date ) TO lt_ftpost.
  ENDIF.

  " Open item selection: one row per invoice (customer side is generated by the clearing)
  LOOP AT lt_refs INTO lv_ref.
    APPEND VALUE #( agkoa  = 'D'
                    agkon  = lv_kunnr
                    agbuk  = iv_bukrs
                    xnops  = 'X'
                    selfd  = iv_match_by
                    selvon = lv_ref ) TO lt_ftclear.
  ENDLOOP.

*--------------------------------------------------------------------*
* 4. Post with clearing (incoming payment, FB05 logic, call transaction)
*--------------------------------------------------------------------*
  CALL FUNCTION 'POSTING_INTERFACE_START'
    EXPORTING
      i_function         = 'C'
      i_mode             = 'N'
      i_update           = 'S'
      i_user             = sy-uname
    EXCEPTIONS
      client_incorrect   = 1
      function_invalid   = 2
      group_name_missing = 3
      mode_invalid       = 4
      update_invalid     = 5
      OTHERS             = 6.
  IF sy-subrc <> 0.
    ev_status  = 'E'.
    ev_message = |POSTING_INTERFACE_START failed (rc { sy-subrc })|.
    RETURN.
  ENDIF.

  CALL FUNCTION 'POSTING_INTERFACE_CLEARING'
    EXPORTING
      i_auglv                    = 'EINGZAHL'
      i_tcode                    = 'FB05'
      i_sgfunct                  = 'C'
    IMPORTING
      e_msgid                    = lv_msgid
      e_msgno                    = lv_msgno
      e_msgty                    = lv_msgty
      e_msgv1                    = lv_msgv1
      e_msgv2                    = lv_msgv2
      e_msgv3                    = lv_msgv3
      e_msgv4                    = lv_msgv4
      e_subrc                    = lv_subrc
    TABLES
      t_blntab                   = lt_blntab
      t_ftclear                  = lt_ftclear
      t_ftpost                   = lt_ftpost
      t_fttax                    = lt_fttax
    EXCEPTIONS
      clearing_procedure_invalid = 1
      clearing_procedure_missing = 2
      table_t041a_empty          = 3
      transaction_code_invalid   = 4
      amount_format_error        = 5
      too_many_line_items        = 6
      company_code_invalid       = 7
      screen_not_found           = 8
      no_authorization           = 9
      OTHERS                     = 10.
  DATA(lv_exc) = sy-subrc.

  CALL FUNCTION 'POSTING_INTERFACE_END'
    EXCEPTIONS
      session_not_processable = 1
      OTHERS                  = 2.

  IF lv_msgid IS NOT INITIAL.
    MESSAGE ID lv_msgid TYPE 'S' NUMBER lv_msgno
      WITH lv_msgv1 lv_msgv2 lv_msgv3 lv_msgv4 INTO ev_message.
  ENDIF.

  READ TABLE lt_blntab INTO ls_blntab INDEX 1.
  IF lv_exc <> 0 OR lv_subrc <> 0 OR ls_blntab-belnr IS INITIAL.
    ev_status = 'E'.
    IF ev_message IS INITIAL.
      ev_message = |Posting failed (exception { lv_exc }, rc { lv_subrc })|.
    ENDIF.
    RETURN.
  ENDIF.

  ev_belnr = ls_blntab-belnr.
  ev_gjahr = ls_blntab-gjahr.

*--------------------------------------------------------------------*
* 5. Confirm every invoice is now cleared by this document
*--------------------------------------------------------------------*
  LOOP AT lt_refs INTO lv_ref.
    CLEAR lv_count.
    CASE iv_match_by.
      WHEN 'XBLNR'.
        SELECT COUNT(*) FROM bsad_view INTO @lv_count
          WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr
            AND augbl = @ev_belnr AND xblnr = @lv_ref.
      WHEN 'ZUONR'.
        SELECT COUNT(*) FROM bsad_view INTO @lv_count
          WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr
            AND augbl = @ev_belnr AND zuonr = @lv_ref.
      WHEN 'BELNR'.
        lv_belnr = lv_ref.
        SELECT COUNT(*) FROM bsad_view INTO @lv_count
          WHERE bukrs = @iv_bukrs AND kunnr = @lv_kunnr
            AND augbl = @ev_belnr AND belnr = @lv_belnr.
    ENDCASE.
    IF lv_count > 0.
      ev_cleared = ev_cleared + 1.
    ENDIF.
  ENDLOOP.

  SELECT SINGLE augbl, augdt FROM bsad_view
    INTO (@ev_augbl, @ev_augdt)
    WHERE bukrs = @iv_bukrs
      AND kunnr = @lv_kunnr
      AND augbl = @ev_belnr.

  IF ev_cleared = lv_total.
    ev_status  = 'S'.
    ev_message = |Document { ev_belnr } posted and cleared { ev_cleared } invoice(s)|.
  ELSE.
    ev_status  = 'W'.
    ev_message = |Document { ev_belnr } posted, { ev_cleared } of { lv_total } invoice(s) found cleared in BSAD - check FBL5N|.
  ENDIF.

ENDFUNCTION.
